# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for the project-file upload lifecycle (AC-03, AC-04, AC-19, AC-20, AC-22, AC-39, AC-40).

These tests talk to the real S3-compatible endpoint of the test stack (MinIO),
because the whole point of the seam is that the presigned PUT the API mints is
accepted by the storage service and that the finalize evidence comes from the
object that was actually stored. Objects are created outside the database
transaction, so each test registers the keys it stores and the session fixture
deletes them afterwards.
"""

# Python imports
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest import mock

# Django imports
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

# Third party imports
import pytest
import requests
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileObject,
    FileVersion,
    Issue,
    Project,
    ProjectMember,
    ProjectStorageUsage,
    State,
    StorageQuota,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage
from plane.throttles.project_file import ProjectFileUploadThrottle

MINIO_ENDPOINT = "http://test-minio:9000"
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"


def upload_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/complete-upload/"


def abort_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/abort-upload/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    """Sign browser-facing URLs against the reachable test endpoint.

    The stack signs with ``MINIO_PUBLIC_ENDPOINT_URL`` (loopback in the developer
    environment), which a container cannot dereference; pointing it at the test
    MinIO service is what lets these tests exercise the real PUT.
    """
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", MINIO_ENDPOINT)
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", MINIO_ENDPOINT)


@pytest.fixture(autouse=True)
def clean_throttle_state():
    """Throttle counters live in the shared cache and outlive a test."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def stored_objects():
    """Collect the object keys a test stores so they can be removed afterwards."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Files Workspace", slug="files-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Files Project", identifier="FILES", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def _member(project, *, email, role, workspace_role=None, is_active=True):
    """Create a user with the given project role (and optionally a workspace role)."""
    local_part = email.split("@")[0]
    user = User.objects.create(email=email, username=local_part, first_name=local_part)
    user.set_password("test-password")
    user.save()

    if workspace_role is not None:
        WorkspaceMember.objects.create(
            workspace=project.workspace, member=user, role=workspace_role, is_active=True
        )
    ProjectMember.objects.create(
        project=project, member=user, workspace=project.workspace, role=role, is_active=is_active
    )
    return user


def _state_for(project):
    return State.objects.create(
        name="Todo",
        color="#60646C",
        group="unstarted",
        project=project,
        workspace=project.workspace,
    )


def _other_project(project, user):
    """A second project the same user may write to, so only the scope can fail."""
    other_project = Project.objects.create(
        name="Other Project", identifier="OTHR", workspace=project.workspace
    )
    ProjectMember.objects.create(
        project=other_project, member=user, workspace=project.workspace, role=20, is_active=True
    )
    return other_project


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _initiate(client, project, **overrides):
    payload = {
        "file_name": "Report.pdf",
        "size_bytes": len(PDF_BYTES),
        "mime_type": "application/pdf",
    }
    payload.update(overrides)
    return client.post(upload_url(project.workspace.slug, project.id), payload, format="json")


def _put(url, headers, data=PDF_BYTES):
    return requests.put(url, data=data, headers=headers, timeout=30)


def _store_directly(key, *, content_type, data=PDF_BYTES):
    """Store an object without the signed URL, the way a hostile client would."""
    storage = S3Storage()
    assert storage.upload_file(io.BytesIO(data), key, content_type=content_type) is True
    return key


@pytest.mark.contract
@pytest.mark.django_db
class TestUploadHappyPath:
    """AC-03: a member gets a presigned PUT bound to the key and content type."""

    def test_presign_put_complete_activates_the_first_version(
        self, session_client, project, stored_objects
    ):
        response = _initiate(session_client, project)
        assert response.status_code == status.HTTP_200_OK, response.data

        file_id = response.data["file"]["id"]
        object_key = response.data["file"]["object_key"]
        upload = response.data["upload"]
        stored_objects.append(object_key)

        # The URL is signed for the exact key and content type.
        assert response.data["version_no"] == 1
        assert upload["method"] == "PUT"
        assert upload["headers"] == {"Content-Type": "application/pdf"}
        assert upload["url"].split("?")[0].endswith(object_key)
        assert "X-Amz-Signature=" in upload["url"]
        assert "content-type" in upload["url"].lower()
        assert datetime.fromisoformat(upload["expires_at"]) > timezone.now()

        put_response = _put(upload["url"], upload["headers"])
        assert put_response.status_code == 200

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        assert complete.status_code == status.HTTP_200_OK, complete.data

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        file_object = FileObject.objects.get(id=file_id)

        assert complete.data["version"]["status"] == FileVersion.Status.ACTIVE
        assert complete.data["activation_required"] is False
        assert version.is_active is True
        assert version.status == FileVersion.Status.ACTIVE
        assert version.size_bytes == len(PDF_BYTES)
        assert version.etag
        assert version.magic_bytes_checked_at is not None
        assert version.storage_metadata["magic_bytes"] == {
            "checked": True,
            "matches": True,
            "declared": "application/pdf",
        }
        assert file_object.status == FileObject.Status.ACTIVE
        assert file_object.current_version_no == 1
        assert file_object.created_by.email == "test@plane.so"
        assert version.uploaded_by.email == "test@plane.so"

        usage = ProjectStorageUsage.objects.get(project=project)
        quota_row = StorageQuota.objects.get(workspace=project.workspace)
        assert usage.used_bytes == len(PDF_BYTES)
        assert usage.reserved_bytes == 0
        assert quota_row.used_bytes == len(PDF_BYTES)
        assert quota_row.reserved_bytes == 0

        assert complete.data["storage_usage"]["project_used_bytes"] == len(PDF_BYTES)

    def test_a_guest_cannot_initiate_an_upload(self, project):
        guest = _member(project, email="guest@example.com", role=5)

        response = _initiate(_client_for(guest), project)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert FileObject.objects.filter(project=project).count() == 0

    def test_a_non_member_cannot_initiate_an_upload(self, project):
        outsider = User.objects.create(email="outsider@example.com", username="outsider")
        outsider.set_password("test-password")
        outsider.save()

        response = _initiate(_client_for(outsider), project)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_complete_upload_scoped_to_another_project_is_not_found(self, session_client, project, create_user):
        other_project = _other_project(project, create_user)
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]

        cross_project = session_client.post(
            complete_url(project.workspace.slug, other_project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert cross_project.status_code == status.HTTP_404_NOT_FOUND

    def test_upload_is_rejected_before_signing_when_it_exceeds_the_file_ceiling(self, session_client, project):
        response = _initiate(session_client, project, size_bytes=settings.PROJECT_FILE_MAX_BYTES + 1)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert FileObject.objects.filter(project=project).count() == 0
        assert FileVersion.objects.filter(project=project).count() == 0

    def test_unsupported_content_type_is_rejected_before_signing(self, session_client, project):
        response = _initiate(session_client, project, mime_type="application/x-msdownload")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_linked_issue_file_lands_under_the_issue_category(self, session_client, project):
        issue = Issue.objects.create(
            name="Attach a report",
            project=project,
            workspace=project.workspace,
            state=_state_for(project),
        )

        response = _initiate(
            session_client,
            project,
            link={"entity_type": "issue", "entity_id": str(issue.id)},
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["file"]["category"] == "issues"
        assert f"/issues/{project.identifier}-{issue.sequence_id}/" in response.data["file"]["object_key"]

    def test_a_link_to_another_project_is_rejected(self, session_client, project):
        other_project = Project.objects.create(
            name="Other Project", identifier="OTHR", workspace=project.workspace
        )
        other_state = _state_for(other_project)
        other_issue = Issue.objects.create(
            name="Elsewhere", project=other_project, workspace=project.workspace, state=other_state
        )

        response = _initiate(
            session_client,
            project,
            link={"entity_type": "issue", "entity_id": str(other_issue.id)},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"


@pytest.mark.contract
@pytest.mark.django_db
class TestFinalizeVerification:
    """AC-04 and AC-39: the object must match the declaration before it is visible."""

    def test_size_mismatch_fails_the_version_and_releases_the_reservation(
        self, session_client, project, stored_objects
    ):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        object_key = response.data["file"]["object_key"]
        stored_objects.append(object_key)
        _store_directly(object_key, content_type="application/pdf")

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES) + 10},
            format="json",
        )

        assert complete.status_code == status.HTTP_400_BAD_REQUEST
        assert complete.data["code"] == "size_mismatch"
        assert complete.data["observed_size"] == len(PDF_BYTES)

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        file_object = FileObject.objects.get(id=file_id)
        assert version.status == FileVersion.Status.FAILED
        assert version.magic_bytes_checked_at is None
        assert file_object.status == FileObject.Status.PENDING
        assert file_object.current_version_no == 0
        assert StorageQuota.objects.get(workspace=project.workspace).reserved_bytes == 0
        assert ProjectStorageUsage.objects.get(project=project).used_bytes == 0
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.UPLOAD_FAILED).count() == 1

    def test_content_type_mismatch_fails_the_version(self, session_client, project, stored_objects):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        object_key = response.data["file"]["object_key"]
        stored_objects.append(object_key)
        # The object was stored with a different content type than declared.
        _store_directly(object_key, content_type="text/plain")

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.status_code == status.HTTP_400_BAD_REQUEST
        assert complete.data["code"] == "mime_mismatch"
        assert complete.data["observed_mime_type"] == "text/plain"
        assert FileVersion.objects.get(file_id=file_id, version_no=1).status == FileVersion.Status.FAILED

    def test_missing_object_fails_cleanly(self, session_client, project):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.status_code == status.HTTP_400_BAD_REQUEST
        assert complete.data["code"] == "object_missing"

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.status == FileVersion.Status.FAILED
        assert version.reservation_released_at is not None
        assert StorageQuota.objects.get(workspace=project.workspace).reserved_bytes == 0

    def test_magic_byte_contradiction_is_not_activated(self, session_client, project, stored_objects):
        """AC-39: a zip stored under a pdf declaration fails instead of activating."""
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        object_key = response.data["file"]["object_key"]
        stored_objects.append(object_key)
        zip_bytes = b"PK\x03\x04\x14\x00\x00\x00"
        _store_directly(object_key, content_type="application/pdf", data=zip_bytes)

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(zip_bytes)},
            format="json",
        )

        assert complete.status_code == status.HTTP_400_BAD_REQUEST
        assert complete.data["code"] == "mime_mismatch"
        assert complete.data["reason"] == "magic_bytes_mismatch"
        # The evidence that contradicted the declaration is recorded, not only
        # the failure code.
        assert complete.data["observed_head_hex"] == zip_bytes[:32].hex()
        assert complete.data["etag"]

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.status == FileVersion.Status.FAILED
        assert version.magic_bytes_checked_at is None
        assert version.is_active is False
        assert version.reserved_bytes == 0
        assert version.storage_metadata["failure"]["observed_head_hex"] == zip_bytes[:32].hex()
        assert version.storage_metadata["failure"]["etag"] == complete.data["etag"]
        assert FileObject.objects.get(id=file_id).current_version_no == 0

    def test_no_second_attempt_while_a_reservation_is_live(self, session_client, project, stored_objects):
        """ARCH-001 §2.4: presigning is refused while a live reservation exists."""
        first = _initiate(session_client, project)
        file_id = first.data["file"]["id"]
        stored_objects.append(first.data["file"]["object_key"])

        second = _initiate(session_client, project, file_id=file_id)

        assert second.status_code == status.HTTP_409_CONFLICT
        assert second.data["code"] == "upload_in_progress"
        assert second.data["version_no"] == 1
        assert FileVersion.objects.filter(file_id=file_id).count() == 1
        assert ProjectStorageUsage.objects.get(project=project).reserved_bytes == len(PDF_BYTES)

        # The refused call must not have disturbed the live attempt.
        _put(first.data["upload"]["url"], first.data["upload"]["headers"])
        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.status_code == status.HTTP_200_OK, complete.data
        assert complete.data["version"]["status"] == FileVersion.Status.ACTIVE
        assert ProjectStorageUsage.objects.get(project=project).reserved_bytes == 0

    def test_retry_of_a_failed_first_upload_activates(self, session_client, project, stored_objects):
        """A retried first upload must be able to complete and become active."""
        first = _initiate(session_client, project)
        file_id = first.data["file"]["id"]

        failed = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        assert failed.data["code"] == "object_missing"

        retry = _initiate(session_client, project, file_id=file_id)
        assert retry.status_code == status.HTTP_200_OK, retry.data
        assert retry.data["version_no"] == 2

        retry_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
        stored_objects.append(retry_key)
        _put(retry.data["upload"]["url"], retry.data["upload"]["headers"])

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 2, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.status_code == status.HTTP_200_OK, complete.data
        assert complete.data["activation_required"] is False
        assert complete.data["version"]["status"] == FileVersion.Status.ACTIVE

        file_object = FileObject.objects.get(id=file_id)
        assert file_object.status == FileObject.Status.ACTIVE
        assert file_object.current_version_no == 2
        assert FileVersion.objects.get(file_id=file_id, version_no=2).is_active is True
        assert ProjectStorageUsage.objects.get(project=project).used_bytes == len(PDF_BYTES)

    def test_a_revision_of_an_active_file_waits_for_activation(self, session_client, project, stored_objects):
        """A revision must not displace the active version without confirmation."""
        first = _initiate(session_client, project)
        file_id = first.data["file"]["id"]
        first_key = first.data["file"]["object_key"]
        stored_objects.append(first_key)
        _put(first.data["upload"]["url"], first.data["upload"]["headers"])
        session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        revision = _initiate(session_client, project, file_id=file_id)
        assert revision.data["version_no"] == 2
        stored_objects.append(FileVersion.objects.get(file_id=file_id, version_no=2).object_key)
        _put(revision.data["upload"]["url"], revision.data["upload"]["headers"])

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 2, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.status_code == status.HTTP_200_OK, complete.data
        assert complete.data["activation_required"] is True
        assert complete.data["version"]["status"] == FileVersion.Status.SUPERSEDED
        assert FileVersion.objects.get(file_id=file_id, version_no=1).is_active is True
        assert FileVersion.objects.get(file_id=file_id, version_no=2).is_active is False

        file_object = FileObject.objects.get(id=file_id)
        assert file_object.current_version_no == 1
        assert file_object.object_key == first_key
        assert ProjectStorageUsage.objects.get(project=project).used_bytes == 2 * len(PDF_BYTES)

    def test_repeated_finalize_after_failure_replays_the_stored_failure(
        self, session_client, project, stored_objects
    ):
        """AC-19: a repeat does not create another version or audit row."""
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        stored_objects.append(response.data["file"]["object_key"])

        complete = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        repeat = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert complete.data["code"] == "object_missing"
        assert repeat.status_code == status.HTTP_400_BAD_REQUEST
        assert repeat.data["code"] == "object_missing"

        assert FileVersion.objects.filter(file_id=file_id).count() == 1
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.UPLOAD_FAILED).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestFinalizeIdempotency:
    """AC-19: finalize settles once and repeats return the stored result."""

    def test_repeated_finalize_returns_the_stored_result(self, session_client, project, stored_objects):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        stored_objects.append(response.data["file"]["object_key"])
        _put(response.data["upload"]["url"], response.data["upload"]["headers"])

        first = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )
        second = session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert second.data["version"] == first.data["version"]
        assert second.data["activation_required"] is False

        assert FileVersion.objects.filter(file_id=file_id, version_no=1).count() == 1
        assert FileAccessLog.objects.filter(
            file_id=file_id, action=FileAccessLog.Action.UPLOAD_COMPLETED
        ).count() == 1
        usage = ProjectStorageUsage.objects.get(project=project)
        assert usage.used_bytes == len(PDF_BYTES)
        assert usage.reserved_bytes == 0


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
class TestConcurrentFinalize:
    """AC-40: two simultaneous finalizes settle the usage exactly once."""

    def test_two_concurrent_finalizes_settle_once(self, session_client, project, stored_objects):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        stored_objects.append(response.data["file"]["object_key"])
        _put(response.data["upload"]["url"], response.data["upload"]["headers"])

        user = User.objects.get(email="test@plane.so")
        barrier = threading.Barrier(2)
        results = []

        def finalize():
            client = _client_for(user)
            barrier.wait(timeout=30)
            result = client.post(
                complete_url(project.workspace.slug, project.id, file_id),
                {"version_no": 1, "size_bytes": len(PDF_BYTES)},
                format="json",
            )
            results.append(result.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(finalize) for _ in range(2)]
            for future in futures:
                future.result(timeout=60)

        assert results == [status.HTTP_200_OK, status.HTTP_200_OK]

        versions = FileVersion.objects.filter(file_id=file_id)
        assert versions.count() == 1
        assert versions.filter(is_active=True).count() == 1
        assert (
            FileAccessLog.objects.filter(
                file_id=file_id, action=FileAccessLog.Action.UPLOAD_COMPLETED
            ).count()
            == 1
        )

        usage = ProjectStorageUsage.objects.get(project=project)
        assert usage.used_bytes == len(PDF_BYTES)
        assert usage.reserved_bytes == 0
        quota_row = StorageQuota.objects.get(workspace=project.workspace)
        assert quota_row.used_bytes == len(PDF_BYTES)
        assert quota_row.reserved_bytes == 0


@pytest.mark.contract
@pytest.mark.django_db
class TestAbortUpload:
    """Abort ends the attempt and releases its reservation exactly once."""

    def test_abort_releases_the_reservation_once(self, session_client, project):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        url = abort_url(project.workspace.slug, project.id, file_id)

        first = session_client.post(url, {"version_no": 1}, format="json")
        second = session_client.post(url, {"version_no": 1}, format="json")

        assert first.status_code == status.HTTP_204_NO_CONTENT
        assert second.status_code == status.HTTP_204_NO_CONTENT

        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.status == FileVersion.Status.FAILED
        assert version.reservation_released_at is not None
        assert StorageQuota.objects.get(workspace=project.workspace).reserved_bytes == 0
        assert ProjectStorageUsage.objects.get(project=project).reserved_bytes == 0
        assert FileAccessLog.objects.filter(
            file_id=file_id, action=FileAccessLog.Action.UPLOAD_FAILED
        ).count() == 1

    def test_abort_after_settlement_is_refused(self, session_client, project, stored_objects):
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]
        stored_objects.append(response.data["file"]["object_key"])
        _put(response.data["upload"]["url"], response.data["upload"]["headers"])
        session_client.post(
            complete_url(project.workspace.slug, project.id, file_id),
            {"version_no": 1, "size_bytes": len(PDF_BYTES)},
            format="json",
        )

        abort = session_client.post(
            abort_url(project.workspace.slug, project.id, file_id), {"version_no": 1}, format="json"
        )

        assert abort.status_code == status.HTTP_409_CONFLICT
        assert abort.data["code"] == "not_uploading"
        assert FileVersion.objects.get(file_id=file_id, version_no=1).status == FileVersion.Status.ACTIVE

    def test_abort_of_another_projects_file_is_not_found(self, session_client, project, create_user):
        other_project = _other_project(project, create_user)
        response = _initiate(session_client, project)
        file_id = response.data["file"]["id"]

        abort = session_client.post(
            abort_url(project.workspace.slug, other_project.id, file_id), {"version_no": 1}, format="json"
        )

        assert abort.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.contract
@pytest.mark.django_db
class TestQuotaEnforcement:
    """The ceiling is enforced before anything is signed."""

    def test_reservation_is_refused_when_the_workspace_quota_is_exhausted(self, session_client, project):
        quota_row, _ = StorageQuota.objects.get_or_create(workspace=project.workspace)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=100, used_bytes=100)

        response = _initiate(session_client, project, size_bytes=50)

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "quota_exceeded"
        assert response.data["level"] == "workspace"
        # Byte counts stay numbers, not strings.
        assert response.data["limit_bytes"] == 100
        assert isinstance(response.data["projected_bytes"], int)
        assert FileObject.objects.filter(project=project).count() == 0
        assert FileVersion.objects.filter(project=project).count() == 0
        assert FileAccessLog.objects.filter(
            project=project, action=FileAccessLog.Action.QUOTA_REJECTED
        ).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestUploadThrottle:
    """AC-22: repeated presigns beyond the throttle return 429."""

    def test_presign_burst_returns_429(self, session_client, project):
        with mock.patch.object(ProjectFileUploadThrottle, "rate", "2/minute", create=True):
            first = _initiate(session_client, project)
            second = _initiate(session_client, project)
            third = _initiate(session_client, project)

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert third.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert third.data["error_message"] == "RATE_LIMIT_EXCEEDED"

    def test_the_throttle_is_scoped_per_project(self, session_client, project):
        other_project = Project.objects.create(
            name="Other Project", identifier="OTHR", workspace=project.workspace
        )
        ProjectMember.objects.create(
            project=other_project,
            member=User.objects.get(email="test@plane.so"),
            workspace=project.workspace,
            role=20,
            is_active=True,
        )

        with mock.patch.object(ProjectFileUploadThrottle, "rate", "1/minute", create=True):
            allowed = _initiate(session_client, project)
            throttled = _initiate(session_client, project)
            other = _initiate(session_client, other_project)

        assert allowed.status_code == status.HTTP_200_OK
        assert throttled.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        # A burst in one project must not starve another (R-UPL-5).
        assert other.status_code == status.HTTP_200_OK
