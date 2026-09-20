# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-108 against the running stack: a revision waits for its confirmation (ADV-001 §4.2).

Both uploads go through the real pipeline (`initiate` -> signed `PUT` -> `complete`),
every object claim is made with an independent boto3 client, and the two facts the
owner decision turns on are asserted directly: the revision is stored `superseded`
with `activation_required: true`, and the active pointer does not move until the
activation call - not even when the revision's upload finalized successfully.
"""

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
UPLOAD_NAME = "smoke-versions.pdf"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def stored_objects():
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
class TestVersionsSmoke:
    """AC-13/AC-43 over HTTP: stored but not active, until the explicit call."""

    def test_a_revision_is_stored_but_not_active_until_activate(
        self, plane_server, create_user, user_data, stored_objects
    ):
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="file-versions-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Versions", slug="smoke-versions", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Versions Files", identifier="SMKV", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)
        files_path = reverse("project-files", kwargs={"slug": workspace.slug, "project_id": project.id})
        initiate_path = reverse(
            "project-file-initiate-upload", kwargs={"slug": workspace.slug, "project_id": project.id}
        )

        file_id = None
        complete_results = {}
        for expected_version in (1, 2):
            payload = {
                "file_name": UPLOAD_NAME,
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
            }
            if file_id is not None:
                payload["file_id"] = file_id

            initiated = request(session, plane_server, "post", initiate_path, token, json=payload)
            assert initiated.status_code == 200, initiated.content[:300]
            file_id = initiated.json()["file"]["id"]
            version_no = initiated.json()["version_no"]
            assert version_no == expected_version

            put = requests.put(
                initiated.json()["upload"]["url"],
                data=PDF_BYTES,
                headers=initiated.json()["upload"]["headers"],
                timeout=60,
            )
            assert put.status_code == 200, put.content[:200]

            key = FileVersion.objects.get(file_id=file_id, version_no=version_no).object_key
            stored_objects.append(key)

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
            complete_results[version_no] = completed.json()

        versions_path = reverse(
            "project-file-versions",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )
        download_path = reverse(
            "project-file-download",
            kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
        )

        # --- the revision is stored superseded and asks to be confirmed
        assert complete_results[1]["activation_required"] is False
        assert complete_results[2]["activation_required"] is True
        assert complete_results[2]["version"]["status"] == FileVersion.Status.SUPERSEDED

        stored_versions = list(FileVersion.objects.filter(file_id=file_id).order_by("version_no"))
        active_rows = [version for version in stored_versions if version.is_active]
        assert len(active_rows) == 1 and active_rows[0].version_no == 1

        store = independent_store()
        for version in stored_versions:
            # Both objects survive: a revision never overwrites its predecessor.
            assert object_length(store, version.object_key) == len(PDF_BYTES)

        history = request(session, plane_server, "get", versions_path, token)
        assert history.status_code == 200, history.content[:300]
        by_number = {row["version_no"]: row for row in history.json()}
        assert by_number[1]["status"] == FileVersion.Status.ACTIVE
        assert by_number[1]["can_activate"] is False
        assert by_number[2]["status"] == FileVersion.Status.SUPERSEDED
        assert by_number[2]["can_activate"] is True

        # The pointer is still on v1, and the download signs v1's object.
        before = FileObject.objects.get(pk=file_id)
        assert before.current_version_no == 1
        assert before.object_key == stored_versions[0].object_key
        signed = request(session, plane_server, "get", download_path, token)
        assert signed.status_code == 200
        assert signed.json()["version_no"] == 1
        assert signed.json()["url"].split("?")[0].endswith(stored_versions[0].object_key)

        # --- the explicit activation moves the pointer
        activated = request(
            session,
            plane_server,
            "post",
            reverse(
                "project-file-version-activate",
                kwargs={
                    "slug": workspace.slug,
                    "project_id": project.id,
                    "file_id": file_id,
                    "version_no": 2,
                },
            ),
            token,
        )
        assert activated.status_code == 200, activated.content[:300]

        stored_versions = list(FileVersion.objects.filter(file_id=file_id).order_by("version_no"))
        active_rows = [version for version in stored_versions if version.is_active]
        assert len(active_rows) == 1 and active_rows[0].version_no == 2
        assert stored_versions[0].status == FileVersion.Status.SUPERSEDED
        assert stored_versions[1].status == FileVersion.Status.ACTIVE

        after = FileObject.objects.get(pk=file_id)
        assert after.current_version_no == 2
        assert after.object_key == stored_versions[1].object_key

        signed = request(session, plane_server, "get", download_path, token)
        assert signed.status_code == 200
        assert signed.json()["version_no"] == 2
        assert signed.json()["url"].split("?")[0].endswith(stored_versions[1].object_key)

        # --- an object deleted out of band is refused, and the pointer stays
        # v1 is the superseded version now: removing its object with the store's own
        # client leaves no trace in the database, which is exactly the case the
        # activation path has to catch by asking the store.
        removed_key = stored_versions[0].object_key
        assert S3Storage().delete_files([removed_key]) is True
        stored_objects.remove(removed_key)
        assert object_length(store, removed_key) is None

        refused = request(
            session,
            plane_server,
            "post",
            reverse(
                "project-file-version-activate",
                kwargs={
                    "slug": workspace.slug,
                    "project_id": project.id,
                    "file_id": file_id,
                    "version_no": 1,
                },
            ),
            token,
        )
        assert refused.status_code == 409, refused.content[:300]
        assert refused.json()["code"] == "object_unavailable"
        assert refused.json()["version_no"] == 1

        still = FileObject.objects.get(pk=file_id)
        assert still.current_version_no == 2, "the pointer must not move onto a missing object"
        assert still.object_key == stored_versions[1].object_key
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 2
        assert FileVersion.objects.get(file_id=file_id, version_no=1).status == FileVersion.Status.SUPERSEDED
