# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Versioning: create a revision, list history, activate (AC-13, AC-43).

Two rules are the reason this file exists in this shape:

* a revision is stored **superseded** and only the activation endpoint moves the
  pointer (AD-18) - so every test that uploads a revision asserts the pointer did
  *not* move;
* the active pointer only moves to a version that can be served - the activation
  path applies the delivery predicate and then the object store, so a version whose
  object was removed outside this application cannot become active.
"""

# Python imports
import uuid
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import requests
from botocore.exceptions import EndpointConnectionError
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
import pytest

from plane.db.models import (
    FileAccessLog,
    FileObject,
    FileVersion,
    Project,
    ProjectMember,
    ProjectStorageUsage,
    StorageQuota,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def versions_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}versions/"


def activate_url(slug, project_id, file_id, version_no):
    return f"{versions_url(slug, project_id, file_id)}{version_no}/activate/"


def download_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}download/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}complete-upload/"


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


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Version Workspace", slug="version-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Version Project", identifier="VERS", workspace=workspace)
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def add_member(project, *, email, role, active=True):
    local = email.split("@")[0]
    user = User.objects.create(email=email, username=local, first_name=local)
    user.set_password("test-password")
    user.save()
    ProjectMember.objects.create(
        project=project, member=user, workspace=project.workspace, role=role, is_active=active
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def initiate(session_client, project, *, name="Report.pdf", file_id=None, size_bytes=None):
    payload = {
        "file_name": name,
        "size_bytes": size_bytes if size_bytes is not None else len(PDF_BYTES),
        "mime_type": "application/pdf",
    }
    if file_id is not None:
        payload["file_id"] = str(file_id)

    return session_client.post(upload_url(project.workspace.slug, project.id), payload, format="json")


def put_and_complete(session_client, project, initiated, *, declared_size=None, stored_objects):
    """Store the bytes at the signed key and finalize; return the complete response."""
    upload = initiated.data["upload"]
    version_no = initiated.data["version_no"]
    file_id = initiated.data["file"]["id"]

    put = requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30)
    assert put.status_code == 200, put.content[:200]

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": version_no, "size_bytes": len(PDF_BYTES) if declared_size is None else declared_size},
        format="json",
    )
    key = FileVersion.objects.get(file_id=file_id, version_no=version_no).object_key
    stored_objects.append(key)

    return completed


def make_versioned_file(session_client, project, *, stored_objects, revisions=0):
    """A file with v1 (active) plus ``revisions`` verified superseded revisions."""
    first = initiate(session_client, project)
    completed = put_and_complete(session_client, project, first, stored_objects=stored_objects)
    assert completed.status_code == status.HTTP_200_OK, completed.data
    assert completed.data["version"]["status"] == FileVersion.Status.ACTIVE
    file_id = first.data["file"]["id"]

    for _ in range(revisions):
        expected_version_no = FileVersion.objects.filter(file_id=file_id).count() + 1
        revision = initiate(session_client, project, file_id=file_id)
        assert revision.data["version_no"] == expected_version_no
        finished = put_and_complete(session_client, project, revision, stored_objects=stored_objects)
        assert finished.status_code == status.HTTP_200_OK, finished.data
        assert finished.data["activation_required"] is True
        assert finished.data["version"]["status"] == FileVersion.Status.SUPERSEDED

    return file_id


def version_key(file_id, version_no):
    return FileVersion.objects.get(file_id=file_id, version_no=version_no).object_key


def usage_bytes(project):
    return (
        ProjectStorageUsage.objects.get(project=project).used_bytes,
        StorageQuota.objects.get(workspace=project.workspace).used_bytes,
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestRevisionLifecycle:
    """AC-43 / R-VER-1: a revision is stored, never silently promoted."""

    def test_a_revision_is_stored_superseded_and_the_pointer_stays(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)

        file_object = FileObject.objects.get(pk=file_id)
        active = FileVersion.objects.filter(file_id=file_id, is_active=True)
        assert active.count() == 1
        assert active.first().version_no == 1
        assert file_object.current_version_no == 1
        assert file_object.object_key == version_key(file_id, 1)
        assert FileVersion.objects.get(file_id=file_id, version_no=2).status == FileVersion.Status.SUPERSEDED
        # Both objects are stored: a revision never overwrites its predecessor.
        for version_no in (1, 2):
            assert S3Storage().get_object_metadata(version_key(file_id, version_no)) is not None

    def test_a_failed_revision_leaves_the_active_version_alone(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)
        revision = initiate(session_client, project, file_id=file_id)
        failed = put_and_complete(
            session_client, project, revision, declared_size=len(PDF_BYTES) + 5, stored_objects=stored_objects
        )

        assert failed.status_code == status.HTTP_400_BAD_REQUEST
        assert failed.data["code"] == "size_mismatch"
        file_object = FileObject.objects.get(pk=file_id)
        assert file_object.current_version_no == 1
        assert file_object.object_key == version_key(file_id, 1)
        assert FileVersion.objects.get(file_id=file_id, version_no=2).status == FileVersion.Status.FAILED
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 1

    def test_the_revision_door_mints_the_next_key_and_keeps_the_older_ones(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        before = {version_key(file_id, 1), version_key(file_id, 2)}

        revision = session_client.post(
            versions_url(project.workspace.slug, project.id, file_id),
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )

        assert revision.status_code == status.HTTP_200_OK, revision.data
        assert revision.data["version_no"] == 3
        assert revision.data["upload"]["method"] == "PUT"
        planned = FileVersion.objects.get(file_id=file_id, version_no=3).object_key
        assert planned not in before
        for key in before:
            assert S3Storage().get_object_metadata(key) is not None

    def test_the_revision_door_refuses_a_body_that_names_another_file(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)
        other = uuid.uuid4()

        named = session_client.post(
            versions_url(project.workspace.slug, project.id, file_id),
            {
                "file_name": "Report.pdf",
                "size_bytes": len(PDF_BYTES),
                "mime_type": "application/pdf",
                "file_id": str(other),
            },
            format="json",
        )
        unknown = session_client.post(
            versions_url(project.workspace.slug, project.id, file_id),
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf", "x": 1},
            format="json",
        )

        for response, field in ((named, "file_id"), (unknown, "x")):
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert response.data["code"] == "unsupported_field"
            assert response.data["field"] == field

        assert FileVersion.objects.filter(file_id=file_id).count() == 1

    def test_a_trashed_file_refuses_a_new_revision(self, session_client, project, stored_objects):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204

        response = session_client.post(
            versions_url(project.workspace.slug, project.id, file_id),
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_trashed"
        assert FileVersion.objects.filter(file_id=file_id).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestVersionHistory:
    """AC-13 / R-VER-3: history with uploader, size, observed ETag and status."""

    def test_history_lists_both_versions_newest_first(
        self, session_client, create_user, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)

        response = session_client.get(versions_url(project.workspace.slug, project.id, file_id))

        assert response.status_code == status.HTTP_200_OK, response.data
        rows = response.data
        assert [row["version_no"] for row in rows] == [2, 1]

        active, superseded = rows[1], rows[0]
        assert active["status"] == FileVersion.Status.ACTIVE
        assert active["is_active"] is True
        assert active["can_activate"] is False  # already the active version
        assert superseded["status"] == FileVersion.Status.SUPERSEDED
        assert superseded["is_active"] is False
        assert superseded["can_activate"] is True
        for row in rows:
            assert row["size_bytes"] == len(PDF_BYTES)
            assert row["etag"], "the observed ETag is part of the history"
            assert row["uploaded_by"]["id"] == str(create_user.id)
            assert row["created_at"] and row["updated_at"]

    def test_history_is_readable_by_a_guest_but_not_by_an_outsider(
        self, session_client, project, stored_objects
    ):
        guest = add_member(project, email="versions-guest@example.com", role=5)
        outsider = add_member(project, email="versions-outsider@example.com", role=20, active=False)
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)

        assert guest.get(versions_url(project.workspace.slug, project.id, file_id)).status_code == status.HTTP_200_OK
        assert (
            outsider.get(versions_url(project.workspace.slug, project.id, file_id)).status_code
            == status.HTTP_404_NOT_FOUND
        )

    def test_history_of_a_trashed_file_needs_the_trash_flag(self, session_client, project, stored_objects):
        member = add_member(project, email="versions-member@example.com", role=15)
        file_id = make_versioned_file(member, project, stored_objects=stored_objects)
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204

        hidden = member.get(versions_url(project.workspace.slug, project.id, file_id))
        flagged = member.get(versions_url(project.workspace.slug, project.id, file_id), {"trashed": True})
        admin = session_client.get(versions_url(project.workspace.slug, project.id, file_id))

        assert hidden.status_code == status.HTTP_404_NOT_FOUND
        assert flagged.status_code == status.HTTP_200_OK
        assert [row["version_no"] for row in flagged.data] == [1]
        # An ADMIN may address the trash without the flag (the documented widener).
        assert admin.status_code == status.HTTP_200_OK

    def test_an_unknown_file_is_not_found(self, session_client, project):
        response = session_client.get(versions_url(project.workspace.slug, project.id, uuid.uuid4()))

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"error": "The required object does not exist."}


@pytest.mark.contract
@pytest.mark.django_db
class TestActivation:
    """AC-43 / R-VER-2 / AD-18: only this call moves the pointer, and only to sane targets."""

    def test_activating_a_revision_moves_the_pointer_and_audits_it(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        revision_key = version_key(file_id, 2)

        response = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["activated"] is True
        assert response.data["previous_version_no"] == 1
        assert response.data["version"]["version_no"] == 2
        assert response.data["file"]["current_version_no"] == 2

        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 2
        assert FileVersion.objects.get(file_id=file_id, version_no=1).status == FileVersion.Status.SUPERSEDED
        file_object = FileObject.objects.get(pk=file_id)
        assert file_object.current_version_no == 2
        assert file_object.object_key == revision_key

        audit = FileAccessLog.objects.get(file_id=file_id, action=FileAccessLog.Action.VERSION_ACTIVATED)
        assert audit.metadata["previous_version_no"] == 1
        assert audit.metadata["size_bytes"] == len(PDF_BYTES)

        signed = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert signed.status_code == status.HTTP_200_OK
        assert signed.data["version_no"] == 2
        assert signed.data["url"].split("?")[0].endswith(revision_key)

    def test_activating_the_active_version_again_changes_nothing(
        self, session_client, project, stored_objects
    ):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        first = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))
        assert first.status_code == status.HTTP_200_OK

        again = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))

        assert again.status_code == status.HTTP_200_OK
        assert again.data["activated"] is False
        assert again.data["previous_version_no"] is None
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 2
        assert (
            FileAccessLog.objects.filter(
                file_id=file_id, action=FileAccessLog.Action.VERSION_ACTIVATED
            ).count()
            == 1
        )

    def test_activating_an_unknown_version_is_not_found(self, session_client, project, stored_objects):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)

        response = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 9))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_activating_a_failed_version_is_refused(self, session_client, project, stored_objects):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)
        revision = initiate(session_client, project, file_id=file_id)
        put_and_complete(
            session_client, project, revision, declared_size=len(PDF_BYTES) + 5, stored_objects=stored_objects
        )

        response = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "object_unavailable"
        assert response.data["version_status"] == FileVersion.Status.FAILED
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 1

    def test_activating_a_version_whose_object_was_removed_out_of_band_is_refused(
        self, session_client, project, stored_objects
    ):
        """The row says the object was stored; only the store says it still is."""
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        revision_key = version_key(file_id, 2)
        assert S3Storage().delete_files([revision_key]) is True
        stored_objects.remove(revision_key)

        response = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "object_unavailable"
        assert response.data["version_no"] == 2
        file_object = FileObject.objects.get(pk=file_id)
        assert file_object.current_version_no == 1
        assert file_object.object_key == version_key(file_id, 1)
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 1
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.VERSION_ACTIVATED).count() == 0

    def test_a_guest_cannot_activate_and_an_outsider_sees_nothing(
        self, session_client, project, stored_objects
    ):
        guest = add_member(project, email="activate-guest@example.com", role=5)
        outsider = add_member(project, email="activate-outsider@example.com", role=20, active=False)
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)

        assert (
            guest.post(activate_url(project.workspace.slug, project.id, file_id, 2)).status_code
            == status.HTTP_403_FORBIDDEN
        )
        assert (
            outsider.post(activate_url(project.workspace.slug, project.id, file_id, 2)).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 1

    def test_an_archived_project_refuses_activation(self, session_client, project, stored_objects):
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())

        response = session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "project_archived"
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).get().version_no == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestPurgedVersionsStayUnselectable:
    """T-107 carry-forward 1: a PURGED version is never active and never selectable."""

    def test_a_partially_purged_file_cannot_be_activated_and_has_no_active_purged_version(
        self, session_client, project, stored_objects
    ):
        """Some versions purged, the row surviving: the shape T-107's F-1 leaves.

        v2 is activated first so the *active* version is the newest one - the purge
        walks newest first, so v2's object goes before v1's deletion fails. That is
        the ordering in which a purge could leave the active pointer on a version
        whose object it just deleted, which is what this test pins.
        """
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects, revisions=1)
        assert session_client.post(activate_url(project.workspace.slug, project.id, file_id, 2)).status_code == 200
        first_key, active_key = version_key(file_id, 1), version_key(file_id, 2)
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204

        real_delete = S3Storage.delete_files
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        def flaky_delete(self, object_names):
            if first_key in object_names:
                raise failure
            return real_delete(self, object_names)

        with mock.patch.object(S3Storage, "delete_files", flaky_delete):
            failed = session_client.delete(
                f"{detail_url(project.workspace.slug, project.id, file_id)}purge/?confirm=true"
            )
        assert failed.status_code == status.HTTP_502_BAD_GATEWAY

        file_object = FileObject.all_objects.get(pk=file_id)
        assert file_object.status == FileObject.Status.PURGE_FAILED

        purged = FileVersion.objects.get(file_id=file_id, version_no=2)
        assert purged.status == FileVersion.Status.PURGED
        assert purged.object_deleted_at is not None
        # The invariant this test exists for: the version whose object the purge
        # removed is no longer the active version, even though it was before.
        assert purged.is_active is False
        assert FileVersion.objects.filter(file_id=file_id, status=FileVersion.Status.PURGED, is_active=True).count() == 0
        assert FileVersion.objects.filter(file_id=file_id, is_active=True).count() == 0
        remaining = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert remaining.status == FileVersion.Status.PURGE_FAILED
        assert S3Storage().get_object_metadata(first_key) is not None  # its deletion failed

        # Nothing can be activated onto this row (the shared write verdict).
        assert (
            session_client.post(activate_url(project.workspace.slug, project.id, file_id, 1)).status_code
            == status.HTTP_404_NOT_FOUND
        )

        # Delivery refuses the file (no active version) and the purged version by
        # number, naming the version's own status.
        file_level = session_client.get(download_url(project.workspace.slug, project.id, file_id))
        assert file_level.status_code == status.HTTP_409_CONFLICT
        assert file_level.data["code"] == "object_unavailable"
        assert "url" not in file_level.data

        by_number = session_client.get(download_url(project.workspace.slug, project.id, file_id), {"version": 2})
        assert by_number.status_code == status.HTTP_409_CONFLICT
        assert by_number.data["code"] == "object_unavailable"
        assert by_number.data["version_status"] == FileVersion.Status.PURGED
        assert "url" not in by_number.data

        # The history tells the truth and advertises no activation for either row.
        rows = {
            row["version_no"]: row
            for row in session_client.get(
                versions_url(project.workspace.slug, project.id, file_id), {"trashed": True}
            ).data
        }
        assert rows[2]["status"] == FileVersion.Status.PURGED
        assert rows[1]["status"] == FileVersion.Status.PURGE_FAILED
        assert rows[1]["can_activate"] is False
        assert rows[2]["can_activate"] is False
        assert active_key != first_key

    def test_a_new_version_cannot_resurrect_a_purge_failed_file(
        self, session_client, project, stored_objects
    ):
        """T-107 carry-forward 3: the accounting of a failed purge is left alone."""
        file_id = make_versioned_file(session_client, project, stored_objects=stored_objects)
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        before_usage = usage_bytes(project)

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            assert (
                session_client.delete(
                    f"{detail_url(project.workspace.slug, project.id, file_id)}purge/?confirm=true"
                ).status_code
                == status.HTTP_502_BAD_GATEWAY
            )
        after_failure_usage = usage_bytes(project)
        assert after_failure_usage == before_usage

        revision = session_client.post(
            versions_url(project.workspace.slug, project.id, file_id),
            {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"},
            format="json",
        )

        assert revision.status_code == status.HTTP_404_NOT_FOUND
        assert revision.data == {"error": "The required object does not exist."}
        assert FileVersion.objects.filter(file_id=file_id).count() == 1
        assert usage_bytes(project) == after_failure_usage
        assert FileVersion.objects.filter(file_id=file_id, status=FileVersion.Status.PURGED, is_active=True).count() == 0
