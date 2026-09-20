# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-110 against the running stack: a reservation becomes usage exactly once (ADV-001 §4.2).

A deliberately tiny workspace limit makes the ceiling observable over HTTP, and every
number is read back with a **fresh query** (T-102 F-5 was a stale in-memory counter)
and compared with what ``storage/`` reports. ``reserved_bytes`` is asserted equal to
0, never ``>= 0``, because the whole point of the single-fire release is that a
reservation cannot survive its attempt.
"""

# Django imports
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

# Third party imports
import pytest
import requests

# Module imports
from plane.db.models import FileObject, FileVersion, Project, ProjectMember, ProjectStorageUsage, StorageQuota, Workspace, WorkspaceMember
from plane.license.models import Instance
from plane.utils.file_storage.quota import accounted_bytes, get_usage_rows

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
#: Small enough that one real upload fits and an over-declared one cannot.
TINY_LIMIT = 512


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


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
class TestStorageQuotaSmoke:
    """AC-15 over HTTP: what is reserved becomes what is used, exactly once."""

    def test_reservation_becomes_usage_exactly_once(self, plane_server, create_user, user_data):
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="storage-quota-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Quota", slug="smoke-quota", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Quota Files", identifier="SMKQ", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)
        storage_path = reverse("project-file-storage", kwargs={"slug": workspace.slug, "project_id": project.id})
        initiate_path = reverse(
            "project-file-initiate-upload", kwargs={"slug": workspace.slug, "project_id": project.id}
        )

        # --- storage/ first, which also materialises the counter rows
        initial = request(session, plane_server, "get", storage_path, token)
        assert initial.status_code == 200, initial.content[:300]
        assert initial.json()["project_used_bytes"] == 0
        assert initial.json()["workspace_used_bytes"] == 0
        assert isinstance(initial.json()["limit_bytes"], int)

        get_usage_rows(project)
        StorageQuota.objects.filter(workspace=workspace).update(limit_bytes=TINY_LIMIT)
        assert request(session, plane_server, "get", storage_path, token).json()["limit_bytes"] == TINY_LIMIT

        # --- an over-limit presign is refused with the limit and moves nothing
        over = request(
            session,
            plane_server,
            "post",
            initiate_path,
            token,
            json={"file_name": "Too big.pdf", "size_bytes": TINY_LIMIT * 10, "mime_type": "application/pdf"},
        )
        assert over.status_code in (400, 409), over.content[:300]
        assert over.json()["code"] == "quota_exceeded"
        assert over.json()["limit_bytes"] == TINY_LIMIT
        assert isinstance(over.json()["limit_bytes"], int)

        usage = ProjectStorageUsage.objects.get(project=project)
        quota = StorageQuota.objects.get(workspace=workspace)
        assert (usage.used_bytes, usage.reserved_bytes) == (0, 0)
        assert (quota.used_bytes, quota.reserved_bytes) == (0, 0)
        assert FileObject.objects.filter(project=project).count() == 0
        assert FileVersion.objects.filter(file__project_id=project.id).count() == 0

        # --- one real file through the pipeline
        initiated = request(
            session,
            plane_server,
            "post",
            initiate_path,
            token,
            json={"file_name": "Fits.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
        )
        assert initiated.status_code == 200, initiated.content[:300]
        file_id = initiated.json()["file"]["id"]
        assert request(session, plane_server, "get", storage_path, token).json()["project_used_bytes"] == 0

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
            json={"version_no": 1, "size_bytes": len(PDF_BYTES)},
        )
        assert completed.status_code == 200, completed.content[:300]

        # --- a third attempt, aborted
        aborted = request(
            session,
            plane_server,
            "post",
            initiate_path,
            token,
            json={
                "file_name": "Abandoned.pdf",
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
            },
        )
        assert aborted.status_code == 200, aborted.content[:300]
        abort_response = request(
            session,
            plane_server,
            "post",
            reverse(
                "project-file-abort-upload",
                kwargs={
                    "slug": workspace.slug,
                    "project_id": project.id,
                    "file_id": aborted.json()["file"]["id"],
                },
            ),
            token,
            json={"version_no": 1},
        )
        assert abort_response.status_code == 204, abort_response.content[:200]

        # --- fresh queries: the counters equal the version rows, and nothing is held
        usage = ProjectStorageUsage.objects.get(project=project)
        quota = StorageQuota.objects.get(workspace=workspace)
        version_sum = accounted_bytes(project)
        assert usage.used_bytes == quota.used_bytes == version_sum == len(PDF_BYTES)
        assert usage.reserved_bytes == 0
        assert quota.reserved_bytes == 0

        abandoned = FileVersion.objects.get(file_id=aborted.json()["file"]["id"], version_no=1)
        assert abandoned.reservation_released_at is not None
        assert abandoned.reserved_bytes == 0
        assert abandoned.status == FileVersion.Status.FAILED

        report = request(session, plane_server, "get", storage_path, token)
        assert report.status_code == 200
        assert report.json()["project_used_bytes"] == usage.used_bytes
        assert report.json()["workspace_used_bytes"] == quota.used_bytes
        assert report.json()["limit_bytes"] == TINY_LIMIT
        assert report.json()["file_count"] == 2  # the stored file and the abandoned attempt
        assert report.json()["version_count"] == 2
