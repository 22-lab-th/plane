# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-106 against the running stack: HTTP front door, real MinIO, second client (ADV-001 §4.2).

The file is created only through the real pipeline (initiate-upload -> signed PUT ->
complete-upload), every object claim is proved with an **independent** boto3 client
rather than the app's adapter, and the recorded state is read back with a fresh
query. That is deliberate: a copy that aliases the source's ``object_key`` would
still return 200 and look correct from the response body alone - it only breaks
when somebody deletes one of the two files. So the assertions here are about
object identity and object existence, not about the JSON.
"""

# Python imports
from unittest import mock

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
from plane.db.models import FileObject, FileVersion, Project, ProjectMember, Workspace, WorkspaceMember
from plane.license.models import Instance
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
UPLOAD_NAME = "smoke-report.pdf"
RENAMED_NAME = "renamed-report.pdf"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    """Point the presigned URL at the store the test process can actually reach."""
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def stored_objects():
    """Delete the objects this test stored; the bucket survives the test run."""
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
    """Fetch a CSRF token for the session (Django rotates it on sign-in)."""
    response = session.get(f"{plane_server.url}{reverse('get_csrf_token')}", timeout=30)
    assert response.status_code == 200
    return response.json()["csrf_token"]


def signed_in_session(plane_server, user_data):
    """Sign in through the real endpoint; the returned session carries the cookie."""
    session = requests.Session()
    sign_in = f"{plane_server.url}{reverse('sign-in')}"
    response = session.post(
        sign_in,
        data={"email": user_data["email"], "password": user_data["password"]},
        headers={"X-CSRFToken": csrf_token(session, plane_server)},
        timeout=30,
        allow_redirects=False,  # the redirect target is the web app, not this stack
    )
    location = response.headers.get("Location", "")
    assert response.status_code in (301, 302), response.content[:200]
    cookie_name = settings.SESSION_COOKIE_NAME
    assert session.cookies.get(cookie_name), (
        f"sign-in did not establish a session (cookie {cookie_name}, redirected to {location})"
    )
    return session


def request(session, plane_server, method, path, token, **kwargs):
    """Call the API with the session cookie and the CSRF header."""
    return getattr(session, method)(
        f"{plane_server.url}{path}", headers={"X-CSRFToken": token}, timeout=60, **kwargs
    )


@pytest.mark.smoke
@pytest.mark.django_db(transaction=True)
class TestFileOpsSmoke:
    """AC-06/AC-10 over HTTP: a rename touches no bytes, a copy mints a new identity."""

    def test_rename_moves_no_bytes_and_copy_mints_a_new_key(
        self, plane_server, create_user, user_data, stored_objects
    ):
        # --- fixture rows: the sign-in provider, one workspace, two projects
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="file-ops-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Ops", slug="smoke-ops", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Files", identifier="SMOKE", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )
        other_project = Project.objects.create(name="Smoke Elsewhere", identifier="SMKE", workspace=workspace)
        ProjectMember.objects.create(
            project=other_project, member=create_user, workspace=workspace, role=20, is_active=True
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)

        files_path = reverse("project-files", kwargs={"slug": workspace.slug, "project_id": project.id})
        folders_path = reverse("project-file-folders", kwargs={"slug": workspace.slug, "project_id": project.id})

        folder_a = request(session, plane_server, "post", folders_path, token, json={"name": "Folder A"})
        folder_b = request(session, plane_server, "post", folders_path, token, json={"name": "Folder B"})
        assert folder_a.status_code == 200, folder_a.content[:200]
        assert folder_b.status_code == 200, folder_b.content[:200]
        folder_a_id = folder_a.json()["folder"]["id"]
        folder_b_id = folder_b.json()["folder"]["id"]

        # --- the file exists only through the real pipeline
        initiated = request(
            session,
            plane_server,
            "post",
            reverse("project-file-initiate-upload", kwargs={"slug": workspace.slug, "project_id": project.id}),
            token,
            json={
                "file_name": UPLOAD_NAME,
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
                "folder_id": folder_a_id,
            },
        )
        assert initiated.status_code == 200, initiated.content[:300]
        body = initiated.json()
        file_id, object_key, upload = body["file"]["id"], body["file"]["object_key"], body["upload"]
        stored_objects.append(object_key)

        put = requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=60)
        assert put.status_code == 200, put.content[:200]

        detail_path = reverse(
            "project-file-detail",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        completed = request(
            session,
            plane_server,
            "post",
            reverse(
                "project-file-complete-upload",
                kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
            ),
            token,
            json={"version_no": 1, "size_bytes": len(PDF_BYTES)},
        )
        assert completed.status_code == 200, completed.content[:300]
        assert completed.json()["version"]["status"] == FileVersion.Status.ACTIVE
        assert completed.json()["activation_required"] is False

        store = independent_store()
        assert object_length(store, object_key) == len(PDF_BYTES)

        # --- the rename must not touch the object store, and the adapter must not be asked to
        with mock.patch.object(S3Storage, "copy_object") as copy_object, mock.patch.object(
            S3Storage, "delete_files"
        ) as delete_files:
            renamed = request(
                session, plane_server, "patch", detail_path, token, json={"name_display": RENAMED_NAME}
            )
        assert renamed.status_code == 200, renamed.content[:300]

        stored = FileObject.objects.get(pk=file_id)  # fresh query, not the response body
        assert stored.name_display == RENAMED_NAME
        assert stored.name_original == UPLOAD_NAME
        assert stored.object_key == object_key, "a rename must not rewrite the key"
        assert object_length(store, object_key) == len(PDF_BYTES), "the object moved during a rename"
        assert copy_object.call_count == 0 and delete_files.call_count == 0

        # --- the copy mints a new identity under this project's prefix
        copy_path = reverse(
            "project-file-copy",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        copied = request(session, plane_server, "post", copy_path, token, json={"folder_id": folder_b_id})
        assert copied.status_code == 200, copied.content[:300]
        copy_id = copied.json()["file"]["id"]
        assert copy_id != file_id

        copy = FileObject.objects.get(pk=copy_id)
        stored_objects.append(copy.object_key)
        assert copy.object_key != object_key, "the copy aliased the source's object"
        storage_key = Project.objects.get(pk=project.id).storage_key
        assert f"workspace/{workspace.slug}/projects/{storage_key}/" in copy.object_key
        assert object_length(store, copy.object_key) == len(PDF_BYTES)
        assert object_length(store, object_key) == len(PDF_BYTES), "the source object disappeared"

        listed = request(session, plane_server, "get", files_path, token, params={"folder_id": folder_b_id})
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["results"]] == [copy_id]

        # a second copy mints a third key: no reuse, no overwrite
        copied_again = request(session, plane_server, "post", copy_path, token, json={"folder_id": folder_b_id})
        assert copied_again.status_code == 200, copied_again.content[:300]
        second_id = copied_again.json()["file"]["id"]
        second_key = FileObject.objects.get(pk=second_id).object_key
        stored_objects.append(second_key)
        assert len({object_key, copy.object_key, second_key}) == 3
        assert object_length(store, second_key) == len(PDF_BYTES)

        # --- cross-project copy is not T-106's to do, and refusing it changes nothing
        rows_before = FileObject.objects.filter(project=project).count()
        cross = request(
            session, plane_server, "post", copy_path, token, json={"target_project_id": str(other_project.id)}
        )
        assert cross.status_code == 400, cross.content[:300]
        assert cross.json()["code"] == "cross_project_not_supported"
        assert FileObject.objects.filter(project=project).count() == rows_before
        assert FileObject.objects.filter(project=other_project).count() == 0
        for key in (object_key, copy.object_key, second_key):
            assert object_length(store, key) == len(PDF_BYTES)
