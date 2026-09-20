# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""T-109 against the running stack: attaching and unlinking an issue (ADV-001 §4.2).

The issue is created through the API, the file through the real upload pipeline, and
the object is proved with an independent boto3 client. The point of the test is the
orphan contract: unlinking the last link removes a row and nothing else - the file
stays listed, stays downloadable, and keeps its bytes (AC-21).
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
from plane.db.models import FileLink, FileObject, FileVersion, Project, ProjectMember, State, Workspace, WorkspaceMember
from plane.license.models import Instance
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
UPLOAD_NAME = "smoke-links.pdf"


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
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION or "us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def object_length(store, key):
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
class TestFileLinksSmoke:
    """AC-16/AC-21 over HTTP: one stored object, two surfaces, an intact orphan."""

    def test_unlinking_the_last_link_leaves_an_intact_orphan(
        self, plane_server, create_user, user_data, stored_objects
    ):
        Instance.objects.create(
            instance_name="Smoke Plane",
            instance_id="file-links-smoke",
            current_version="1.0.0",
            last_checked_at=timezone.now(),
            is_setup_done=True,
        )
        workspace = Workspace.objects.create(name="Smoke Links", slug="smoke-links", owner=create_user)
        WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
        project = Project.objects.create(name="Smoke Links Files", identifier="SMKL", workspace=workspace)
        ProjectMember.objects.create(
            project=project, member=create_user, workspace=workspace, role=20, is_active=True
        )
        other_project = Project.objects.create(name="Smoke Elsewhere", identifier="SMKE", workspace=workspace)
        ProjectMember.objects.create(
            project=other_project, member=create_user, workspace=workspace, role=20, is_active=True
        )
        state = State.objects.create(
            name="Todo", color="#60646C", group="unstarted", project=project, workspace=workspace
        )

        session = signed_in_session(plane_server, user_data)
        token = csrf_token(session, plane_server)

        # --- an issue, created through the API
        issue_response = request(
            session,
            plane_server,
            "post",
            reverse("project-issue", kwargs={"slug": workspace.slug, "project_id": project.id}),
            token,
            json={"name": "Attach the smoke file", "state_id": str(state.id)},
        )
        assert issue_response.status_code in (200, 201), issue_response.content[:300]
        issue_id = issue_response.json()["id"]
        issue_identifier = f"{project.identifier}-{issue_response.json()['sequence_id']}"

        files_path = reverse("project-files", kwargs={"slug": workspace.slug, "project_id": project.id})

        # --- the file, through the real pipeline, linked to that issue at creation
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
                "link": {"entity_type": "issue", "entity_id": issue_id},
            },
        )
        assert initiated.status_code == 200, initiated.content[:300]
        file_id = initiated.json()["file"]["id"]
        assert initiated.json()["file"]["category"] == "issues"

        put = requests.put(
            initiated.json()["upload"]["url"],
            data=PDF_BYTES,
            headers=initiated.json()["upload"]["headers"],
            timeout=60,
        )
        assert put.status_code == 200, put.content[:200]

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        object_key = version.object_key
        stored_objects.append(object_key)
        assert f"/issues/{issue_identifier}/" in object_key, "the key embeds the entityRef"

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

        link = FileLink.objects.get(file_id=file_id)
        assert link.entity_identifier == issue_identifier
        assert str(link.entity_id) == issue_id

        # --- the same file in both surfaces, with one link and one object
        by_entity = request(
            session,
            plane_server,
            "get",
            files_path,
            token,
            params={"entity_type": "issue", "entity_id": issue_id},
        )
        assert by_entity.status_code == 200, by_entity.content[:300]
        assert [row["id"] for row in by_entity.json()["results"]] == [file_id]
        assert by_entity.json()["results"][0]["link_count"] == 1

        listing = request(session, plane_server, "get", files_path, token)
        assert [row["id"] for row in listing.json()["results"]].count(file_id) == 1

        detail_path = reverse(
            "project-file-detail", kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id}
        )
        detail = request(session, plane_server, "get", detail_path, token)
        assert detail.json()["link_count"] == 1

        # --- a duplicate link is refused, and one live row remains
        links_path = reverse(
            "project-file-links", kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id}
        )
        duplicate = request(
            session, plane_server, "post", links_path, token, json={"entity_type": "issue", "entity_id": issue_id}
        )
        assert duplicate.status_code == 409, duplicate.content[:300]
        assert duplicate.json()["code"] == "link_exists"
        assert FileLink.objects.filter(file_id=file_id).count() == 1

        # --- an entity from another project is refused, with no row written
        foreign_state = State.objects.create(
            name="Todo", color="#60646C", group="unstarted", project=other_project, workspace=workspace
        )
        foreign_issue = request(
            session,
            plane_server,
            "post",
            reverse("project-issue", kwargs={"slug": workspace.slug, "project_id": other_project.id}),
            token,
            json={"name": "Elsewhere", "state_id": str(foreign_state.id)},
        )
        assert foreign_issue.status_code in (200, 201), foreign_issue.content[:300]

        refused = request(
            session,
            plane_server,
            "post",
            links_path,
            token,
            json={"entity_type": "issue", "entity_id": foreign_issue.json()["id"]},
        )
        assert refused.status_code == 400, refused.content[:300]
        assert refused.json()["code"] == "invalid_request"
        assert FileLink.objects.filter(file_id=file_id).count() == 1

        # --- a second, legitimate link, then unlink both
        page_link = request(
            session,
            plane_server,
            "post",
            links_path,
            token,
            json={"entity_type": "project", "entity_id": str(project.id)},
        )
        assert page_link.status_code == 200, page_link.content[:300]
        assert FileLink.objects.filter(file_id=file_id).count() == 2

        for row in FileLink.objects.filter(file_id=file_id):
            removed = request(
                session,
                plane_server,
                "delete",
                f"{links_path}{row.id}/",
                token,
            )
            assert removed.status_code == 204, removed.content[:200]

        # --- the orphan: still one row, zero links, still download-able, object intact
        assert FileLink.objects.filter(file_id=file_id).count() == 0
        assert FileLink.all_objects.filter(file_id=file_id).count() == 2
        assert FileObject.objects.filter(pk=file_id).exists() is True

        detail = request(session, plane_server, "get", detail_path, token)
        assert detail.status_code == 200
        assert detail.json()["link_count"] == 0
        assert detail.json()["links"] == []

        listing = request(session, plane_server, "get", files_path, token)
        rows = {row["id"]: row for row in listing.json()["results"]}
        assert file_id in rows
        assert rows[file_id]["link_count"] == 0

        signed = request(
            session,
            plane_server,
            "get",
            reverse(
                "project-file-download",
                kwargs={"slug": workspace.slug, "project_id": project.id, "file_id": file_id},
            ),
            token,
        )
        assert signed.status_code == 200, signed.content[:300]
        store = independent_store()
        assert object_length(store, object_key) == len(PDF_BYTES)
