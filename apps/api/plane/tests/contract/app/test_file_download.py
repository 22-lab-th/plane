# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for presigned download and preview (AC-07, AC-08, AC-28, AC-34).

The disposition assertions fetch the signed URL with a plain HTTP client and read
the response header: what the browser would receive is the only thing that proves
AC-08/AC-28, so the tests never settle for a string check against the URL.
"""

# Python imports
import io
import json
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

# Django imports
from django.utils import timezone

# Third party imports
import pytest
import requests
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileObject,
    FileVersion,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage
from plane.utils.file_storage.naming import normalize_name

MINIO_ENDPOINT = "http://test-minio:9000"

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
HTML_BYTES = b"<html><body><script>alert(1)</script></body></html>"


def download_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/download/"


def preview_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/preview/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    """Sign browser-facing URLs against the reachable test endpoint."""
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", MINIO_ENDPOINT)
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", MINIO_ENDPOINT)


@pytest.fixture
def stored_objects():
    """Delete the objects a test stores (the database rolls back, the bucket does not)."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Delivery Workspace", slug="delivery-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Delivery Project", identifier="DLVR", workspace=workspace)
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20, is_active=True)
    return project


def add_member(project, *, email, role, active=True):
    user = User.objects.create(email=email, username=email.split("@")[0], first_name=email.split("@")[0])
    user.set_password("test-password")
    user.save()
    WorkspaceMember.objects.create(workspace=project.workspace, member=user, role=role, is_active=True)
    ProjectMember.objects.create(
        project=project, member=user, workspace=project.workspace, role=role, is_active=active
    )
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_file_with_object(
    project,
    *,
    name,
    mime,
    content,
    stored_objects,
    version_no=1,
    status=FileVersion.Status.ACTIVE,
    is_active=True,
    object_deleted=False,
    file_status=FileObject.Status.ACTIVE,
):
    """Create a file plus one version and store real bytes for it."""
    object_key = f"workspace/{project.workspace.slug}/projects/DLVR/docs/{uuid.uuid4()}/v{version_no}/{name}"
    assert S3Storage().upload_file(io.BytesIO(content), object_key, content_type=mime) is True
    stored_objects.append(object_key)

    file_object = FileObject(
        project=project,
        name_original=name,
        name_display=name,
        name_normalized=normalize_name(name),
        mime_type=mime,
        extension=name.rpartition(".")[2],
        bucket="uploads",
        size_bytes=len(content),
        object_key=object_key,
        category=FileObject.Category.DOCS,
        status=file_status,
        current_version_no=version_no,
    )
    file_object.save(force_insert=True, created_by_id=project.created_by_id)

    version = FileVersion.objects.create(
        project=project,
        file=file_object,
        version_no=version_no,
        object_key=object_key,
        bucket="uploads",
        mime_type=mime,
        size_bytes=len(content),
        status=status,
        is_active=is_active,
        object_deleted_at=timezone.now() if object_deleted else None,
    )
    return file_object, version


def fetch(url):
    return requests.get(url, timeout=30)


@pytest.mark.contract
@pytest.mark.django_db
class TestDownloadDisposition:
    """AC-07 and AC-08: what the client actually receives when it fetches."""

    @pytest.mark.parametrize(
        "name,mime,content",
        [
            ("diagram.svg", "image/svg+xml", SVG_BYTES),
            ("notes.html", "text/html", HTML_BYTES),
            ("report.pdf", "application/pdf", PDF_BYTES),
            ("notes.txt", "text/plain", b"plain notes\n"),
        ],
    )
    def test_download_forces_attachment_and_returns_the_bytes(
        self, session_client, project, stored_objects, name, mime, content
    ):
        file_object, _ = make_file_with_object(
            project, name=name, mime=mime, content=content, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["disposition"] == "attachment"
        assert response.data["file_name"] == name
        assert response.data["version_no"] == 1
        assert response.data["url"].split("?")[0].endswith("v1/" + name)
        # The signed disposition is what the store enforces.
        assert "response-content-disposition" in response.data["url"]

        fetched = fetch(response.data["url"])

        assert fetched.status_code == status.HTTP_200_OK
        assert fetched.headers["Content-Disposition"].startswith("attachment")
        assert fetched.content == content

    @pytest.mark.parametrize("mime", ["image/svg+xml", "text/html", "text/javascript", "application/json"])
    def test_preview_never_renders_active_content_inline(
        self, session_client, project, stored_objects, mime
    ):
        content = SVG_BYTES if mime == "image/svg+xml" else HTML_BYTES
        file_object, _ = make_file_with_object(
            project, name=f"active-{uuid.uuid4().hex[:6]}.bin", mime=mime, content=content, stored_objects=stored_objects
        )

        response = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["disposition"] == "attachment"

        fetched = fetch(response.data["url"])

        assert fetched.headers["Content-Disposition"].startswith("attachment")
        # The served type is the verified one, never a type the client chose.
        assert fetched.headers["Content-Type"].startswith(mime.split("/")[0])

    @pytest.mark.parametrize("name,mime,content", [("shot.png", "image/png", PNG_BYTES), ("spec.pdf", "application/pdf", PDF_BYTES)])
    def test_preview_is_inline_only_for_inert_types(
        self, session_client, project, stored_objects, name, mime, content
    ):
        file_object, _ = make_file_with_object(
            project, name=name, mime=mime, content=content, stored_objects=stored_objects
        )

        response = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["disposition"] == "inline"

        fetched = fetch(response.data["url"])

        assert fetched.status_code == status.HTTP_200_OK
        assert fetched.headers["Content-Disposition"].startswith("inline")
        assert fetched.content == content

    def test_an_unlisted_type_previews_as_attachment(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project,
            name="archive.zip",
            mime="application/zip",
            content=b"PK\x03\x04\x14\x00\x00\x00",
            stored_objects=stored_objects,
        )

        response = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert response.data["disposition"] == "attachment"

    def test_the_storage_origin_is_not_the_application_origin(self, session_client, project, stored_objects):
        """Inline content renders on the storage origin, never the app's (R-DL-3).

        The Django-served JSON does carry ``nosniff``; the presigned response
        cannot, which is why origin separation plus forced attachment carries the
        guarantee instead.
        """
        file_object, _ = make_file_with_object(
            project, name="origin.png", mime="image/png", content=PNG_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))
        url = urlsplit(response.data["url"])

        assert url.netloc == urlsplit(MINIO_ENDPOINT).netloc
        assert url.netloc != "testserver"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_only_the_signed_response_parameters_are_used(self, session_client, project, stored_objects):
        """A presigned GET controls only its signed response-* parameters (R-DL-3).

        Header control (nosniff/CSP/CORP) cannot be attached to a presigned
        response and is therefore not claimed anywhere; the only delivery
        controls this endpoint has are the signed ``response-content-disposition``
        and ``response-content-type``, which is what this asserts.
        """
        file_object, _ = make_file_with_object(
            project, name="docs.html", mime="text/html", content=HTML_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))
        query = parse_qs(urlsplit(response.data["url"]).query)
        signed_parameters = {key for key in query if key.startswith("response-")}

        assert signed_parameters == {"response-content-disposition", "response-content-type"}
        assert query["response-content-disposition"][0].startswith("attachment")
        # The body makes no header-control claim either.
        assert set(response.data) == {"url", "expires_at", "disposition", "file_name", "version_no"}

        fetched = fetch(response.data["url"])

        assert fetched.headers["Content-Disposition"].startswith("attachment")


@pytest.mark.contract
@pytest.mark.django_db
class TestDownloadTtl:
    """AC-07: the TTL comes from SIGNED_URL_EXPIRATION and is hard-capped."""

    def test_configured_short_ttl_is_honoured(self, session_client, project, stored_objects, monkeypatch):
        monkeypatch.setenv("SIGNED_URL_EXPIRATION", "120")
        file_object, _ = make_file_with_object(
            project, name="short.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert f"X-Amz-Expires=120" in response.data["url"]
        expires_at = datetime.fromisoformat(response.data["expires_at"])
        assert timedelta(seconds=100) < expires_at - timezone.now() <= timedelta(seconds=125)

    def test_ttl_is_capped_at_the_sigv4_maximum(self, session_client, project, stored_objects, monkeypatch):
        monkeypatch.setenv("SIGNED_URL_EXPIRATION", "9999999")
        file_object, _ = make_file_with_object(
            project, name="long.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert "X-Amz-Expires=604800" in response.data["url"]
        expires_at = datetime.fromisoformat(response.data["expires_at"])
        assert expires_at - timezone.now() <= timedelta(seconds=604800)

    def test_an_expired_url_is_rejected_by_the_store(
        self, session_client, project, stored_objects, monkeypatch
    ):
        """A URL that outlives its TTL stops working, which bounds revocation."""
        monkeypatch.setenv("SIGNED_URL_EXPIRATION", "2")
        file_object, _ = make_file_with_object(
            project, name="expiring.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert "X-Amz-Expires=2" in response.data["url"]
        assert fetch(response.data["url"]).status_code == status.HTTP_200_OK

        time.sleep(3)

        expired = fetch(response.data["url"])

        assert expired.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
class TestVersionSelection:
    """A requested version must have a live object, or the call is refused."""

    def test_version_selection_returns_the_requested_bytes(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="v1.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )
        second_key = f"{file_object.object_key}.v2"
        assert S3Storage().upload_file(io.BytesIO(b"%PDF-1.7\nsecond\n"), second_key, content_type="application/pdf")
        stored_objects.append(second_key)
        FileVersion.objects.create(
            project=project,
            file=file_object,
            version_no=2,
            object_key=second_key,
            bucket="uploads",
            mime_type="application/pdf",
            size_bytes=16,
            status=FileVersion.Status.SUPERSEDED,
            is_active=False,
        )

        active = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))
        requested = session_client.get(
            download_url(project.workspace.slug, project.id, file_object.id), {"version": 2}
        )

        assert active.data["version_no"] == 1
        assert fetch(active.data["url"]).content == PDF_BYTES
        assert requested.data["version_no"] == 2
        assert fetch(requested.data["url"]).content == b"%PDF-1.7\nsecond\n"

    def test_unknown_version_is_not_found(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="only.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(
            download_url(project.workspace.slug, project.id, file_object.id), {"version": 7}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_a_malformed_version_is_rejected(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="malformed.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(
            download_url(project.workspace.slug, project.id, file_object.id), {"version": "one"}
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"

    @pytest.mark.parametrize(
        "status_value,object_deleted",
        [
            (FileVersion.Status.PURGED, True),
            (FileVersion.Status.PURGE_FAILED, True),
            (FileVersion.Status.FAILED, False),
            (FileVersion.Status.UPLOADING, False),
        ],
    )
    def test_a_version_without_a_live_object_is_refused(
        self, session_client, project, stored_objects, status_value, object_deleted
    ):
        file_object, _ = make_file_with_object(
            project,
            name=f"gone-{status_value}.pdf",
            mime="application/pdf",
            content=PDF_BYTES,
            stored_objects=stored_objects,
            status=status_value,
            is_active=False,
            object_deleted=object_deleted,
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "object_unavailable"
        assert response.data["version_status"] == status_value

    def test_a_trashed_file_is_not_served(self, session_client, project, stored_objects):
        """A trashed file is restored before it is downloaded or previewed."""
        file_object, _ = make_file_with_object(
            project, name="trashed.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects,
            file_status=FileObject.Status.TRASHED,
        )

        download = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))
        preview = session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        for response in (download, preview):
            assert response.status_code == status.HTTP_409_CONFLICT
            assert response.data["code"] == "file_trashed"

    def test_a_quarantined_file_is_refused(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project,
            name="quarantined.pdf",
            mime="application/pdf",
            content=PDF_BYTES,
            stored_objects=stored_objects,
            file_status=FileObject.Status.QUARANTINED,
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_quarantined"


@pytest.mark.contract
@pytest.mark.django_db
class TestDeliveryAuditAndAccess:
    """AC-34: one audit row per issuance, no URL in it, and access is recorded."""

    def test_download_writes_one_audit_row_without_the_url(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="audited.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        rows = FileAccessLog.objects.filter(file_id=file_object.id, action=FileAccessLog.Action.DOWNLOADED)
        assert rows.count() == 1

        row = rows.first()
        assert row.actor.email == "test@plane.so"
        assert row.version_no == 1
        assert row.project_id == project.id
        assert row.workspace_id == project.workspace_id
        assert row.metadata["disposition"] == "attachment"
        assert set(row.metadata) == {"disposition", "content_type", "expires_at"}

        serialised = json.dumps(row.metadata)
        assert "X-Amz" not in serialised
        assert response.data["url"] not in serialised
        assert "http" not in serialised

    def test_preview_writes_its_own_action(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="previewed.png", mime="image/png", content=PNG_BYTES, stored_objects=stored_objects
        )

        session_client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert FileAccessLog.objects.filter(
            file_id=file_object.id, action=FileAccessLog.Action.PREVIEWED
        ).count() == 1
        assert FileAccessLog.objects.filter(
            file_id=file_object.id, action=FileAccessLog.Action.DOWNLOADED
        ).count() == 0

    def test_last_accessed_at_moves(self, session_client, project, stored_objects):
        file_object, _ = make_file_with_object(
            project, name="accessed.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )
        assert file_object.last_accessed_at is None

        before = timezone.now()
        session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        file_object.refresh_from_db()
        assert file_object.last_accessed_at is not None
        assert file_object.last_accessed_at >= before - timedelta(seconds=5)

    def test_a_guest_may_download_and_preview(self, project, stored_objects):
        guest = add_member(project, email="guest-delivery@example.com", role=5)
        file_object, _ = make_file_with_object(
            project, name="guest.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )
        client = client_for(guest)

        download = client.get(download_url(project.workspace.slug, project.id, file_object.id))
        preview = client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert download.status_code == status.HTTP_200_OK
        assert preview.status_code == status.HTTP_200_OK
        assert fetch(download.data["url"]).status_code == status.HTTP_200_OK

    def test_a_non_member_gets_the_generic_404(self, project, stored_objects):
        outsider = add_member(project, email="outsider-delivery@example.com", role=20, active=False)
        file_object, _ = make_file_with_object(
            project, name="private.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )
        client = client_for(outsider)

        download = client.get(download_url(project.workspace.slug, project.id, file_object.id))
        preview = client.get(preview_url(project.workspace.slug, project.id, file_object.id))

        assert download.status_code == status.HTTP_404_NOT_FOUND
        assert download.data == {"error": "The required object does not exist."}
        assert preview.status_code == status.HTTP_404_NOT_FOUND

    def test_a_file_from_another_project_is_not_found(self, session_client, project, create_user, stored_objects):
        other_project = Project.objects.create(
            name="Other Delivery", identifier="OTHR", workspace=project.workspace
        )
        ProjectMember.objects.create(
            project=other_project, member=create_user, workspace=project.workspace, role=20, is_active=True
        )
        file_object, _ = make_file_with_object(
            other_project, name="foreign.pdf", mime="application/pdf", content=PDF_BYTES, stored_objects=stored_objects
        )

        response = session_client.get(download_url(project.workspace.slug, project.id, file_object.id))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"error": "The required object does not exist."}
