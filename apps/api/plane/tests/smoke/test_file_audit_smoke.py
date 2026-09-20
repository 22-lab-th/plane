# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-111 against the running stack: the trail of a real session (ADV-001 §4.2).

The whole flow runs over HTTP with a session and CSRF, and then the trail is read the
way an operator would read it: the project activity, a filtered page, and the file's
own history in the detail payload. The scan for a presigned URL is done on the
**serialised JSON** of every row, not on a list of key names, because a signature is a
substring away from being missed.
"""

# Python imports
import ipaddress
import json

# Django imports
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

# Third party imports
import pytest
import requests

# Module imports
from plane.db.models import FileAccessLog, Project, ProjectMember, Workspace, WorkspaceMember
from plane.license.models import Instance

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
UPLOAD_NAME = "smoke-audit.pdf"
USER_AGENT = "audit-smoke/1.0"
#: Anything that would leak the signed URL into the trail.
FORBIDDEN = ("Signature=", "X-Amz-Signature", "test-minio", "http://", "https://")


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
    headers = {"X-CSRFToken": token, "User-Agent": USER_AGENT}
    return getattr(session, method)(f"{plane_server.url}{path}", headers=headers, timeout=60, **kwargs)


@pytest.mark.smoke
@pytest.mark.django_db(transaction=True)
class TestFileAuditSmoke:
    """AC-18/AC-34 over HTTP: one complete row per mutation, and never a URL."""

    def test_every_mutation_writes_one_audit_row_without_the_url(
        self, plane_server, create_user, user_data
    ):
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="file-audit-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Audit", slug="smoke-audit", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Audit Files", identifier="SMKA", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)
        files_path = reverse("project-files", kwargs={"slug": workspace.slug, "project_id": project.id})
        activity_path = reverse("project-file-activity", kwargs={"slug": workspace.slug, "project_id": project.id})

        # --- the flow: initiate, PUT, complete, rename, download, trash, restore
        initiated = request(
            session,
            plane_server,
            "post",
            reverse("project-file-initiate-upload", kwargs={"slug": workspace.slug, "project_id": project.id}),
            token,
            json={"file_name": UPLOAD_NAME, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
        )
        assert initiated.status_code == 200, initiated.content[:300]
        file_id = initiated.json()["file"]["id"]

        put = requests.put(
            initiated.json()["upload"]["url"],
            data=PDF_BYTES,
            headers=initiated.json()["upload"]["headers"],
            timeout=60,
        )
        assert put.status_code == 200, put.content[:200]

        detail_path = reverse(
            "project-file-detail", kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id}
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

        renamed = request(
            session, plane_server, "patch", detail_path, token, json={"name_display": "renamed-audit.pdf"}
        )
        assert renamed.status_code == 200, renamed.content[:300]

        downloaded = request(
            session,
            plane_server,
            "get",
            reverse(
                "project-file-download",
                kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
            ),
            token,
        )
        assert downloaded.status_code == 200, downloaded.content[:300]

        assert request(session, plane_server, "delete", detail_path, token).status_code == 204
        assert request(
            session,
            plane_server,
            "post",
            reverse(
                "project-file-restore",
                kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
            ),
            token,
        ).status_code == 200

        # --- the trail, as an operator reads it
        activity = request(session, plane_server, "get", activity_path, token)
        assert activity.status_code == 200, activity.content[:300]
        rows = activity.json()["results"]
        actions = [row["action"] for row in rows]
        for expected in ("upload_initiated", "upload_completed", "renamed", "downloaded", "trashed", "restored"):
            assert actions.count(expected) == 1, actions
        assert "downloaded" in actions, "AC-34: issuance is in the trail"

        for row in rows:
            assert row["actor"]["id"] == str(create_user.id)
            assert row["actor"]["display_name"]
            assert row["created_at"]
            if row["ip_address"] is not None:
                ipaddress.ip_address(row["ip_address"])
            assert row["user_agent"] == USER_AGENT, row["action"]
            serialised = json.dumps(row)
            for forbidden in FORBIDDEN:
                assert forbidden not in serialised, f"{row['action']}: {serialised}"

        # --- the filter, and the detail's own history
        renamed_rows = request(session, plane_server, "get", activity_path, token, params={"action": "renamed"})
        assert renamed_rows.status_code == 200
        assert [row["action"] for row in renamed_rows.json()["results"]] == ["renamed"]
        assert renamed_rows.json()["page"]["total_results"] == 1

        detail = request(session, plane_server, "get", detail_path, token)
        assert detail.status_code == 200
        db_rows = list(
            FileAccessLog.objects.filter(file_id=file_id)
            .order_by("-created_at", "-id")
            .values_list("id", "action")
        )
        assert [(row["id"], row["action"]) for row in detail.json()["activity"]] == [
            (str(row_id), action) for row_id, action in db_rows
        ]

        # --- append-only through the API
        row_id = rows[0]["id"]
        for method in ("patch", "put", "delete"):
            attempt = getattr(session, method)(
                f"{plane_server.url}{activity_path}{row_id}/",
                headers={"X-CSRFToken": token, "User-Agent": USER_AGENT},
                json={} if method != "delete" else None,
                timeout=60,
            )
            assert attempt.status_code in (404, 405), (method, attempt.status_code)
        assert FileAccessLog.objects.filter(id=row_id).exists() is True
        before = FileAccessLog.objects.filter(file_id=file_id).count()

        # --- and the trail survives a purge
        assert request(session, plane_server, "delete", detail_path, token).status_code == 204
        purged = request(
            session,
            plane_server,
            "delete",
            reverse(
                "project-file-purge", kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id}
            ),
            token,
            params={"confirm": "true"},
        )
        assert purged.status_code == 204, purged.content[:200]

        survivors = FileAccessLog.objects.filter(file_id=file_id)
        assert survivors.count() == before + 2  # the trash and the purge themselves
        assert survivors.filter(action=FileAccessLog.Action.PURGED).count() == 1
        assert request(session, plane_server, "get", detail_path, token).status_code == 404
        assert (
            request(session, plane_server, "get", activity_path, token, params={"file_id": file_id})
            .json()["page"]["total_results"]
            == before + 2
        )
