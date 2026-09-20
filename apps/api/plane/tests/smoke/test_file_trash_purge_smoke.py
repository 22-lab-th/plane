# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-107 against the running stack: trash, restore and purge (ADV-001 §4.2).

The file is created through the real pipeline and every object claim is made with
an **independent** boto3 client. The assertion order is the point of the test: the
purge is only correct if the objects are gone *before* the row is, because
asserting the row first is exactly what lets "row without object" pass.
"""

# Python imports
import uuid

# Django imports
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

# Third party imports
import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileLink,
    FileObject,
    FileVersion,
    Project,
    ProjectMember,
    Workspace,
    WorkspaceMember,
)
from plane.license.models import Instance
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
UPLOAD_NAME = "smoke-trash.pdf"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def stored_objects():
    """Delete whatever objects this test stored; the bucket outlives the test."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


def independent_store():
    """A second boto3 client, built from settings, not the app's adapter."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION or "us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def object_length(store, key):
    """Return the stored length at an exact key, or ``None`` when nothing is stored."""
    try:
        return store.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)["ContentLength"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def csrf_token(session, plane_server):
    response = session.get(f"{plane_server.url}{reverse('get_csrf_token')}", timeout=30)
    assert response.status_code == 200
    return response.json()["csrf_token"]


def signed_in_session(plane_server, user_data):
    """Sign in through the real endpoint; the session carries the cookie afterwards."""
    session = requests.Session()
    response = session.post(
        f"{plane_server.url}{reverse('sign-in')}",
        data={"email": user_data["email"], "password": user_data["password"]},
        headers={"X-CSRFToken": csrf_token(session, plane_server)},
        timeout=30,
        allow_redirects=False,  # the redirect target is the web app, not this stack
    )
    location = response.headers.get("Location", "")
    assert response.status_code in (301, 302), response.content[:200]
    assert session.cookies.get(settings.SESSION_COOKIE_NAME), f"sign-in failed (redirected to {location})"
    return session


def request(session, plane_server, method, path, token, **kwargs):
    return getattr(session, method)(
        f"{plane_server.url}{path}", headers={"X-CSRFToken": token}, timeout=60, **kwargs
    )


@pytest.mark.smoke
@pytest.mark.django_db(transaction=True)
class TestTrashPurgeSmoke:
    """AC-11/AC-12/AC-27 over HTTP: trash keeps the bytes, purge removes them first."""

    def test_trash_keeps_the_object_and_purge_deletes_every_version(
        self, plane_server, create_user, user_data, stored_objects
    ):
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="file-trash-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Trash", slug="smoke-trash", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Trash Files", identifier="SMKT", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)
        files_path = reverse("project-files", kwargs={"slug": workspace.slug, "project_id": project.id})
        folders_path = reverse("project-file-folders", kwargs={"slug": workspace.slug, "project_id": project.id})
        folder = request(session, plane_server, "post", folders_path, token, json={"name": "Folder A"})
        assert folder.status_code == 200, folder.content[:200]
        folder_id = folder.json()["folder"]["id"]

        # --- two versions through the real pipeline, so "every version" means two
        keys = []
        file_id = None
        for _ in range(2):
            payload = {
                "file_name": UPLOAD_NAME,
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
                "folder_id": folder_id,
            }
            if file_id is not None:
                payload["file_id"] = file_id

            initiated = request(
                session,
                plane_server,
                "post",
                reverse("project-file-initiate-upload", kwargs={"slug": workspace.slug, "project_id": project.id}),
                token,
                json=payload,
            )
            assert initiated.status_code == 200, initiated.content[:300]
            file_id = initiated.json()["file"]["id"]
            version_no = initiated.json()["version_no"]

            put = requests.put(
                initiated.json()["upload"]["url"],
                data=PDF_BYTES,
                headers=initiated.json()["upload"]["headers"],
                timeout=60,
            )
            assert put.status_code == 200, put.content[:200]

            completed = request(
                session,
                plane_server,
                "post",
                reverse(
                    "project-file-complete-upload",
                    kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
                ),
                token,
                json={"version_no": version_no, "size_bytes": len(PDF_BYTES)},
            )
            assert completed.status_code == 200, completed.content[:300]

            key = FileVersion.objects.get(file_id=file_id, version_no=version_no).object_key
            keys.append(key)
            stored_objects.append(key)

        assert len(keys) == 2
        store = independent_store()
        for key in keys:
            assert object_length(store, key) == len(PDF_BYTES)

        detail_path = reverse(
            "project-file-detail",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        download_path = reverse(
            "project-file-download",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        restore_path = reverse(
            "project-file-restore",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        purge_path = reverse(
            "project-file-purge",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )

        # --- trash: the bytes stay, delivery refuses, the trash view shows it
        trashed = request(session, plane_server, "delete", detail_path, token)
        assert trashed.status_code == 204, trashed.content[:200]

        stored = FileObject.all_objects.get(pk=file_id)
        assert stored.status == FileObject.Status.TRASHED
        assert stored.deleted_at is not None
        for key in keys:
            assert object_length(store, key) == len(PDF_BYTES), "trash must keep every object"

        refused = request(session, plane_server, "get", download_path, token)
        assert refused.status_code == 409, refused.content[:200]
        assert refused.json()["code"] == "file_trashed"

        default = request(session, plane_server, "get", files_path, token)
        assert default.status_code == 200
        assert file_id not in [row["id"] for row in default.json()["results"]]
        trash_view = request(session, plane_server, "get", files_path, token, params={"trashed": True})
        assert trash_view.status_code == 200
        assert file_id in [row["id"] for row in trash_view.json()["results"]]

        # --- restore: back to its folder, audit row written, delivery works again
        restored = request(session, plane_server, "post", restore_path, token)
        assert restored.status_code == 200, restored.content[:300]
        assert restored.json()["file"]["folder_id"] == folder_id
        assert restored.json()["restore"]["folder_fallback"] is False

        stored = FileObject.all_objects.get(pk=file_id)
        assert stored.status == FileObject.Status.ACTIVE
        assert stored.deleted_at is None
        assert stored.folder_id == uuid.UUID(folder_id)
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.RESTORED).count() == 1
        signed = request(session, plane_server, "get", download_path, token)
        assert signed.status_code == 200, signed.content[:300]
        assert "X-Amz-Signature" in signed.json()["url"]

        # --- purge: every version object first, then the row, then the trail stays
        purged = request(session, plane_server, "delete", detail_path, token)
        assert purged.status_code == 204
        purged = request(session, plane_server, "delete", purge_path, token, params={"confirm": "true"})
        assert purged.status_code == 204, purged.content[:200]

        for key in keys:
            assert object_length(store, key) is None, f"{key} survived the purge"

        assert FileObject.all_objects.filter(pk=file_id).exists() is False
        assert FileObject.objects.filter(pk=file_id).exists() is False
        assert FileVersion.objects.filter(file_id=file_id).count() == 0
        assert FileLink.all_objects.filter(file_id=file_id).count() == 0

        audit = FileAccessLog.objects.get(file_id=file_id, action=FileAccessLog.Action.PURGED)
        assert set(audit.metadata["object_keys"]) == set(keys)
        assert audit.metadata["versions"] == 2

        assert request(session, plane_server, "get", download_path, token).status_code == 404
