# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Trash, restore, purge and the retention task (AC-11, AC-12, AC-27, AC-36).

The assertions are ordered on purpose. A purge is only correct if the *objects*
are gone before the *row* is: asserting the row first is what lets "row without
object" - and its mirror, "object without row" - pass. Every object claim here is
made with an independent boto3 client, and the retention task is run as the real
function (never ``.delay()``: the queue is not part of this environment).
"""

# Python imports
import requests
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import boto3
import pytest
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from django.conf import settings
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.bgtasks.file_purge_task import purge_expired_files
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileLink,
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
from plane.utils.file_storage.naming import normalize_name
from plane.utils.file_storage.purge import purge_expired_batch

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/complete-upload/"


def restore_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/restore/"


def purge_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/purge/"


def download_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/download/"


def folders_url(slug, project_id):
    return f"{files_url(slug, project_id)}folders/"


def folder_url(slug, project_id, folder_id):
    return f"{folders_url(slug, project_id)}{folder_id}/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def independent_store():
    """A second boto3 client: object claims must not go through the app's adapter."""
    return boto3.client(
        "s3",
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION or "us-east-1",
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def object_exists(store, key):
    """True when the exact key is stored; a missing key is a real answer, not an error."""
    try:
        store.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def object_exists_through_adapter(key):
    """The app adapter's own view, for the one place a test wants to contrast it."""
    return S3Storage().get_object_metadata(key) is not None


@contextmanager
def per_key_delete_error():
    """Answer every ``DeleteObjects`` with a 200 that names a key it did not delete.

    S3 reports per-key failures *inside* a 200 response, so a caller that reads only the
    status code reports a deletion that did not happen. The patch sits at the botocore
    call - where that answer is actually produced - rather than at the adapter's own
    helper, so the test exercises the real response handling (F-1).
    """
    real_api_call = BaseClient._make_api_call

    def fake_api_call(self, operation_name, api_params):
        if operation_name != "DeleteObjects":
            return real_api_call(self, operation_name, api_params)

        keys = [obj["Key"] for obj in api_params["Delete"]["Objects"]]
        return {
            "Deleted": [],
            "Errors": [{"Key": key, "Code": "InternalError", "Message": "the key was not deleted"} for key in keys],
        }

    with mock.patch.object(BaseClient, "_make_api_call", fake_api_call):
        yield


@pytest.fixture
def stored_objects(independent_store):
    """Delete whatever objects a test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Trash Workspace", slug="trash-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Trash Project", identifier="TRSH", workspace=workspace)
    # ADMIN by default: purge is an admin-only door, and the tests that need a
    # MEMBER or a GUEST build their own caller explicitly.
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def add_member(project, *, email, role):
    """Create a user with the given project role and return an authenticated client."""
    local = email.split("@")[0]
    user = User.objects.create(email=email, username=local, first_name=local)
    user.set_password("test-password")
    user.save()
    ProjectMember.objects.create(
        project=project, member=user, workspace=project.workspace, role=role, is_active=True
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_folder(project, name, parent=None, depth=None):
    return FileFolder.objects.create(
        project=project,
        parent=parent,
        name=name,
        name_normalized=normalize_name(name),
        depth=depth if depth is not None else ((parent.depth + 1) if parent else 0),
    )


def make_state(project):
    return State.objects.create(
        name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
    )


def upload_file(session_client, project, *, name="Report.pdf", folder=None, stored_objects, link=None):
    """Create one verified version through the real pipeline and return ``(file_id, key)``."""
    payload = {"file_name": name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"}
    if folder is not None:
        payload["folder_id"] = str(folder.id)
    if link is not None:
        payload["link"] = {"entity_type": FileLink.EntityType.ISSUE, "entity_id": str(link.id)}

    initiated = session_client.post(upload_url(project.workspace.slug, project.id), payload, format="json")
    assert initiated.status_code == status.HTTP_200_OK, initiated.data

    file_id = initiated.data["file"]["id"]

    # The signed URL is called with plain requests: it is not an API route.
    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    assert completed.data["version"]["status"] == FileVersion.Status.ACTIVE

    # Read the key from the version row: the initiate payload's ``file.object_key``
    # is the file's canonical (active) key, which is a different thing for a revision.
    object_key = FileVersion.objects.get(file_id=file_id, version_no=1).object_key
    stored_objects.append(object_key)

    return file_id, object_key


def add_revision(session_client, project, file_id, *, stored_objects):
    """Store a second, superseded version and return its key."""
    initiated = session_client.post(
        upload_url(project.workspace.slug, project.id),
        {"file_name": "Report.pdf", "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf", "file_id": file_id},
        format="json",
    )
    assert initiated.status_code == status.HTTP_200_OK, initiated.data

    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 2, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data
    assert completed.data["version"]["status"] == FileVersion.Status.SUPERSEDED

    object_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
    stored_objects.append(object_key)

    return object_key


def trash(session_client, project, file_id):
    return session_client.delete(detail_url(project.workspace.slug, project.id, file_id))


def usage_bytes(project):
    """Read both counters with fresh queries, as the advisory's R-3 check demands."""
    return (
        ProjectStorageUsage.objects.get(project=project).used_bytes,
        StorageQuota.objects.get(workspace=project.workspace).used_bytes,
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestTrash:
    """AC-11 / R-DEL-1: the row moves, the bytes stay, the links go inactive."""

    def test_trash_sets_both_markers_keeps_the_objects_and_unlinks(
        self, session_client, project, stored_objects, independent_store
    ):
        issue = Issue.objects.create(
            name="Attached", project=project, workspace=project.workspace, state=make_state(project)
        )
        file_object, object_key = upload_file(
            session_client, project, stored_objects=stored_objects, link=issue
        )
        before_usage = usage_bytes(project)
        assert before_usage[0] == len(PDF_BYTES)

        response = trash(session_client, project, file_object)

        assert response.status_code == status.HTTP_204_NO_CONTENT
        stored = FileObject.all_objects.get(pk=file_object)
        assert stored.status == FileObject.Status.TRASHED
        assert stored.deleted_at is not None
        # The object is untouched and the counters did not move (AD-09).
        assert object_exists(independent_store, object_key) is True
        assert usage_bytes(project) == before_usage

        link = FileLink.all_objects.get(file_id=file_object)
        assert link.deleted_at is not None, "a trashed file's links are marked inactive"
        assert FileLink.objects.filter(file_id=file_object).count() == 0

        audit = FileAccessLog.objects.get(file_id=file_object, action=FileAccessLog.Action.TRASHED)
        assert audit.metadata["previous_status"] == FileObject.Status.ACTIVE
        assert audit.metadata["link_ids"] == [str(link.id)]
        assert audit.metadata["versions"] == 1

        default = session_client.get(files_url(project.workspace.slug, project.id))
        assert file_object not in [row["id"] for row in default.data["results"]]
        trashed = session_client.get(files_url(project.workspace.slug, project.id), {"trashed": True})
        assert [row["id"] for row in trashed.data["results"]] == [file_object]

    def test_trashing_twice_is_refused_and_changes_nothing(self, session_client, project, stored_objects):
        file_object, _ = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT

        again = trash(session_client, project, file_object)

        assert again.status_code == status.HTTP_409_CONFLICT
        assert again.data["code"] == "file_trashed"
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.TRASHED).count() == 1

    def test_a_guest_cannot_trash_and_a_non_member_sees_nothing(
        self, session_client, project, stored_objects
    ):
        guest = add_member(project, email="trash-guest@example.com", role=5)
        outsider = add_member(project, email="trash-outsider@example.com", role=5)
        ProjectMember.objects.filter(project=project, member__email="trash-outsider@example.com").update(
            is_active=False
        )
        file_object, _ = upload_file(session_client, project, stored_objects=stored_objects)

        assert trash(guest, project, file_object).status_code == status.HTTP_403_FORBIDDEN
        assert trash(outsider, project, file_object).status_code == status.HTTP_404_NOT_FOUND
        assert FileObject.objects.get(pk=file_object).status == FileObject.Status.ACTIVE

    def test_an_archived_project_refuses_the_trash(self, session_client, project, stored_objects):
        file_object, _ = upload_file(session_client, project, stored_objects=stored_objects)
        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())

        response = trash(session_client, project, file_object)

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "project_archived"


    def test_the_trash_surface_lists_a_file_whose_folder_is_gone(
        self, session_client, project, stored_objects
    ):
        """R-DEL-3: a file trashed through its folder keeps its (now dead) folder_id.

        The trash surface is the *project's*, not one folder's: it answers with the row
        whether or not a folder scope is asked for, which is what the Files tab relies on
        when its Trash quick view insists on no folder at all (T-118 F-1). A folder-scoped
        trash request is a different question - one folder's trash - and the folder it
        names no longer exists, so it is a 404 rather than an empty page.
        """
        outer = make_folder(project, "Ghost")
        file_object, _ = upload_file(
            session_client, project, folder=outer, stored_objects=stored_objects
        )

        assert (
            session_client.delete(
                folder_url(project.workspace.slug, project.id, outer.id) + "?recursive=true"
            ).status_code
            == status.HTTP_204_NO_CONTENT
        )

        stored = FileObject.all_objects.get(pk=file_object)
        assert stored.status == FileObject.Status.TRASHED
        assert str(stored.folder_id) == str(outer.id), "the row still names the folder it was in"

        trashed = session_client.get(files_url(project.workspace.slug, project.id), {"trashed": True})
        assert [row["id"] for row in trashed.data["results"]] == [file_object]
        assert str(trashed.data["results"][0]["folder_id"]) == str(outer.id)

        # The root's own trash is a narrower question, and this row is not in it.
        root_trash = session_client.get(
            files_url(project.workspace.slug, project.id), {"trashed": True, "folder_id": "root"}
        )
        assert [row["id"] for row in root_trash.data["results"]] == []

        # Nor is "the deleted folder's trash" the project's trash: that folder no longer
        # resolves, so the endpoint says so instead of answering an empty page.
        gone = session_client.get(
            files_url(project.workspace.slug, project.id),
            {"trashed": True, "folder_id": str(outer.id)},
        )
        assert gone.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.contract
@pytest.mark.django_db
class TestRestore:
    """AC-11 / R-DEL-2: back to its folder, links revived, delivery works again."""

    def test_restore_returns_the_file_to_its_folder_and_revives_its_links(
        self, session_client, project, stored_objects, independent_store
    ):
        folder = make_folder(project, "Reports")
        issue = Issue.objects.create(
            name="Attached", project=project, workspace=project.workspace, state=make_state(project)
        )
        file_object, object_key = upload_file(
            session_client, project, folder=folder, stored_objects=stored_objects, link=issue
        )
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        refused = session_client.get(download_url(project.workspace.slug, project.id, file_object))
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "file_trashed"

        response = session_client.post(restore_url(project.workspace.slug, project.id, file_object))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["restore"] == {"restored_links": 1, "folder_fallback": False}

        stored = FileObject.all_objects.get(pk=file_object)
        assert stored.status == FileObject.Status.ACTIVE
        assert stored.deleted_at is None
        assert stored.folder_id == folder.id
        assert object_exists(independent_store, object_key) is True
        assert FileLink.objects.filter(file_id=file_object).count() == 1
        assert FileLink.all_objects.get(file_id=file_object).deleted_at is None

        audit = FileAccessLog.objects.get(file_id=file_object, action=FileAccessLog.Action.RESTORED)
        assert audit.metadata["restored_links"] == 1
        assert audit.metadata["folder_id"] == str(folder.id)

        assert session_client.get(download_url(project.workspace.slug, project.id, file_object)).status_code == 200
        detail = session_client.get(detail_url(project.workspace.slug, project.id, file_object))
        assert detail.data["permissions"]["can_download"] is True
        assert detail.data["link_count"] == 1

    def test_restore_lands_at_the_project_root_when_the_folder_is_gone(
        self, session_client, project, stored_objects
    ):
        outer = make_folder(project, "Outer")
        inner = make_folder(project, "Inner", parent=outer)
        file_object, object_key = upload_file(session_client, project, folder=inner, stored_objects=stored_objects)

        # Deleting the folder trashes its whole subtree (T-105), the file included.
        deleted = session_client.delete(folder_url(project.workspace.slug, project.id, outer.id) + "?recursive=true")
        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        assert FileObject.all_objects.get(pk=file_object).status == FileObject.Status.TRASHED

        response = session_client.post(restore_url(project.workspace.slug, project.id, file_object))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["restore"]["folder_fallback"] is True
        stored = FileObject.all_objects.get(pk=file_object)
        assert stored.folder_id is None
        assert stored.deleted_at is None
        audit = FileAccessLog.objects.get(file_id=file_object, action=FileAccessLog.Action.RESTORED)
        assert audit.metadata["folder_fallback"] is True
        assert audit.metadata["folder_id"] is None
        listing = session_client.get(files_url(project.workspace.slug, project.id))
        assert [row["id"] for row in listing.data["results"]] == [file_object]

    def test_restore_refuses_when_the_name_was_taken_while_trashed(
        self, session_client, project, stored_objects
    ):
        file_object, _ = upload_file(session_client, project, name="Same.pdf", stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        upload_file(session_client, project, name="Same.pdf", stored_objects=stored_objects)

        response = session_client.post(restore_url(project.workspace.slug, project.id, file_object))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_name_conflict"
        stored = FileObject.all_objects.get(pk=file_object)
        assert stored.status == FileObject.Status.TRASHED
        assert stored.deleted_at is not None
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.RESTORED).count() == 0

    def test_restoring_a_live_file_is_refused(self, session_client, project, stored_objects):
        file_object, _ = upload_file(session_client, project, stored_objects=stored_objects)

        response = session_client.post(restore_url(project.workspace.slug, project.id, file_object))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_not_trashed"
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.RESTORED).count() == 0

    def test_a_link_whose_entity_is_gone_stays_unlinked(self, session_client, project, stored_objects):
        issue = Issue.objects.create(
            name="Doomed", project=project, workspace=project.workspace, state=make_state(project)
        )
        file_object, _ = upload_file(session_client, project, stored_objects=stored_objects, link=issue)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT

        Issue.objects.filter(pk=issue.pk).delete()

        response = session_client.post(restore_url(project.workspace.slug, project.id, file_object))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["restore"]["restored_links"] == 0
        assert FileLink.objects.filter(file_id=file_object).count() == 0
        assert FileLink.all_objects.get(file_id=file_object).deleted_at is not None


@pytest.mark.contract
@pytest.mark.django_db
class TestPurge:
    """AC-12 / AC-27: objects first, audit event, then the row - never the other way."""

    def test_purge_deletes_every_version_object_before_the_row(
        self, session_client, project, stored_objects, independent_store
    ):
        issue = Issue.objects.create(
            name="Attached", project=project, workspace=project.workspace, state=make_state(project)
        )
        file_object, first_key = upload_file(
            session_client, project, stored_objects=stored_objects, link=issue
        )
        second_key = add_revision(session_client, project, file_object, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        assert usage_bytes(project)[0] == 2 * len(PDF_BYTES)

        response = session_client.delete(purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        # Objects first: this assertion must come before the row assertions.
        for key in (first_key, second_key):
            assert object_exists(independent_store, key) is False, key

        assert FileObject.all_objects.filter(pk=file_object).exists() is False
        assert FileVersion.objects.filter(file_id=file_object).count() == 0
        assert FileLink.all_objects.filter(file_id=file_object).count() == 0

        audit = FileAccessLog.objects.get(file_id=file_object, action=FileAccessLog.Action.PURGED)
        assert audit.metadata["trigger"] == "manual"
        assert audit.metadata["versions"] == 2
        assert audit.metadata["bytes"] == 2 * len(PDF_BYTES)
        assert set(audit.metadata["object_keys"]) == {first_key, second_key}

        assert usage_bytes(project) == (0, 0)

        assert session_client.get(download_url(project.workspace.slug, project.id, file_object)).status_code == 404
        assert (
            session_client.get(detail_url(project.workspace.slug, project.id, file_object)).status_code == 404
        )
        trashed = session_client.get(files_url(project.workspace.slug, project.id), {"trashed": True})
        assert file_object not in [row["id"] for row in trashed.data["results"]]

    def test_purge_needs_confirmation_and_the_admin_role(self, session_client, project, stored_objects):
        member = add_member(project, email="purge-member@example.com", role=15)
        guest = add_member(project, email="purge-guest@example.com", role=5)
        outsider = add_member(project, email="purge-outsider@example.com", role=5)
        ProjectMember.objects.filter(project=project, member__email="purge-outsider@example.com").update(
            is_active=False
        )

        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        admin = add_member(project, email="purge-admin@example.com", role=20)

        missing = admin.delete(purge_url(project.workspace.slug, project.id, file_object))
        assert missing.status_code == status.HTTP_400_BAD_REQUEST
        assert missing.data["code"] == "confirmation_required"
        assert missing.data["field"] == "confirm"

        unconfirmed = admin.delete(purge_url(project.workspace.slug, project.id, file_object) + "?confirm=false")
        assert unconfirmed.status_code == status.HTTP_400_BAD_REQUEST
        assert unconfirmed.data["code"] == "confirmation_required"

        unknown_field = admin.delete(
            purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true",
            {"hard": "yes"},
            format="json",
        )
        assert unknown_field.status_code == status.HTTP_400_BAD_REQUEST
        assert unknown_field.data["code"] == "unsupported_field"

        for client, expected in (
            (member, status.HTTP_403_FORBIDDEN),
            (guest, status.HTTP_403_FORBIDDEN),
            (outsider, status.HTTP_404_NOT_FOUND),
        ):
            refused = client.delete(purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true")
            assert refused.status_code == expected

        assert FileObject.all_objects.filter(pk=file_object).exists() is True
        assert object_exists_through_adapter(object_key) is True
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.PURGED).count() == 0

    def test_purging_a_live_file_is_refused(self, session_client, project, stored_objects, independent_store):
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)

        response = session_client.delete(purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_not_trashed"
        assert FileObject.all_objects.filter(pk=file_object).exists() is True
        assert object_exists(independent_store, object_key) is True

    def test_a_failed_object_deletion_leaves_a_purge_failed_row_and_is_retried(
        self, session_client, project, stored_objects, independent_store
    ):
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            response = session_client.delete(
                purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true"
            )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.data["code"] == "storage_unavailable"

        failed = FileObject.all_objects.get(pk=file_object)
        assert failed.status == FileObject.Status.PURGE_FAILED
        assert failed.deleted_at is not None
        assert FileVersion.objects.get(file_id=file_object).status == FileVersion.Status.PURGE_FAILED
        assert object_exists(independent_store, object_key) is True
        assert usage_bytes(project)[0] == len(PDF_BYTES)
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.PURGED).count() == 0

        # The trash surface still shows it, so a failed purge cannot hide.
        trashed = session_client.get(files_url(project.workspace.slug, project.id), {"trashed": True})
        assert file_object in [row["id"] for row in trashed.data["results"]]

        # The next run retries it, however young the row is.
        summary = purge_expired_files(batch_size=10)

        assert summary == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=file_object).exists() is False
        assert object_exists(independent_store, object_key) is False
        assert usage_bytes(project) == (0, 0)
        assert FileAccessLog.objects.filter(
            file_id=file_object, action=FileAccessLog.Action.PURGED
        ).get().metadata["trigger"] == "retention"

    def test_a_per_key_delete_error_is_a_failure_not_a_deleted_object(
        self, session_client, project, stored_objects, independent_store
    ):
        """F-1: ``DeleteObjects`` answers 200 while naming the keys it did not delete.

        Reading only the status code reports a deletion that did not happen: the row
        would go, the quota would be released and a ``PURGED`` audit row would claim
        bytes are gone while the object is still stored. The failure has to surface as
        the ordinary retryable ``purge_failed`` state instead (R3-02, R-DEL-4).
        """
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT

        with per_key_delete_error():
            response = session_client.delete(
                purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true"
            )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY, response.data
        assert response.data["code"] == "storage_unavailable"

        failed = FileObject.all_objects.get(pk=file_object)
        assert failed.status == FileObject.Status.PURGE_FAILED
        assert failed.deleted_at is not None
        assert FileVersion.objects.get(file_id=file_object).status == FileVersion.Status.PURGE_FAILED
        # The object is still there (the provider refused it), so nothing may claim it
        # was deleted: not the counters, not the audit trail.
        assert object_exists(independent_store, object_key) is True
        assert usage_bytes(project)[0] == len(PDF_BYTES)
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.PURGED).count() == 0

        # And the row is retryable: once the provider deletes again, the purge finishes.
        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=file_object).exists() is False
        assert object_exists(independent_store, object_key) is False
        assert usage_bytes(project) == (0, 0)

    def test_a_raising_object_deletion_is_a_failure_too_not_a_500(
        self, session_client, project, stored_objects, independent_store
    ):
        """F-1: a storage client raises outside ClientError on a connection error."""
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        with mock.patch.object(S3Storage, "delete_files", side_effect=failure):
            response = session_client.delete(
                purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true"
            )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY, response.data
        assert response.data["code"] == "storage_unavailable"

        failed = FileObject.all_objects.get(pk=file_object)
        assert failed.status == FileObject.Status.PURGE_FAILED
        assert failed.deleted_at is not None
        assert FileVersion.objects.get(file_id=file_object).status == FileVersion.Status.PURGE_FAILED
        assert object_exists(independent_store, object_key) is True
        assert usage_bytes(project)[0] == len(PDF_BYTES)
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.PURGED).count() == 0

        # And the retry, once the store answers again, still finishes the job.
        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=file_object).exists() is False
        assert object_exists(independent_store, object_key) is False
        assert usage_bytes(project) == (0, 0)

    def test_one_bad_row_does_not_abort_the_batch(self, session_client, project, stored_objects):
        """F-1: the rest of the batch still runs when one row's deletion raises."""
        first, first_key = upload_file(session_client, project, name="First.pdf", stored_objects=stored_objects)
        second, second_key = upload_file(session_client, project, name="Second.pdf", stored_objects=stored_objects)
        for file_id in (first, second):
            assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        # Strictly older, so the failing row is certainly the one the batch reaches
        # first: only then does a purged second row prove the loop continued.
        FileObject.all_objects.filter(pk=first).update(deleted_at=timezone.now() - timedelta(days=32))
        FileObject.all_objects.filter(pk=second).update(deleted_at=timezone.now() - timedelta(days=31))

        real_delete = S3Storage.delete_files
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        def flaky_delete(self, object_names):
            if first_key in object_names:
                raise failure
            return real_delete(self, object_names)

        # Patched as a context manager, so the fixture's own cleanup (which deletes
        # the surviving object) runs against the real method again.
        with mock.patch.object(S3Storage, "delete_files", flaky_delete):
            summary = purge_expired_files(batch_size=10)

        assert summary == {"purged": 1, "failed": 1, "scanned": 2}
        # The bad row is left retryable, with its object and its bytes intact.
        assert FileObject.all_objects.get(pk=first).status == FileObject.Status.PURGE_FAILED
        assert usage_bytes(project)[0] == len(PDF_BYTES)
        # The other row was still purged in the same run.
        assert FileObject.all_objects.filter(pk=second).exists() is False
        assert FileVersion.objects.filter(file_id=second).count() == 0
        assert FileAccessLog.objects.filter(file_id=second, action=FileAccessLog.Action.PURGED).count() == 1

    def test_purge_is_idempotent(self, session_client, project, stored_objects, independent_store):
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        url = purge_url(project.workspace.slug, project.id, file_object) + "?confirm=true"
        assert session_client.delete(url).status_code == status.HTTP_204_NO_CONTENT

        again = session_client.delete(url)

        assert again.status_code == status.HTTP_404_NOT_FOUND
        assert FileAccessLog.objects.filter(file_id=file_object, action=FileAccessLog.Action.PURGED).count() == 1
        assert usage_bytes(project) == (0, 0)
        assert object_exists(independent_store, object_key) is False

@pytest.mark.contract
@pytest.mark.django_db
class TestRetentionPurgeTask:
    """AC-12 / R-DEL-4: the schedule purges past the window and retries failures."""

    def test_only_files_past_their_window_are_purged(
        self, session_client, project, stored_objects, independent_store
    ):
        expired, expired_key = upload_file(session_client, project, name="Old.pdf", stored_objects=stored_objects)
        fresh, fresh_key = upload_file(session_client, project, name="New.pdf", stored_objects=stored_objects)
        assert trash(session_client, project, expired).status_code == status.HTTP_204_NO_CONTENT
        assert trash(session_client, project, fresh).status_code == status.HTTP_204_NO_CONTENT

        # Backdate one of them past the default 30 day window.
        FileObject.all_objects.filter(pk=expired).update(deleted_at=timezone.now() - timedelta(days=31))

        summary = purge_expired_files(batch_size=10)

        assert summary == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=expired).exists() is False
        assert object_exists(independent_store, expired_key) is False
        assert FileObject.all_objects.filter(pk=fresh).exists() is True
        assert object_exists(independent_store, fresh_key) is True

    def test_a_project_window_overrides_the_default(self, session_client, project, stored_objects):
        Project.objects.filter(pk=project.pk).update(retention_days=365)
        kept, kept_key = upload_file(session_client, project, name="Kept.pdf", stored_objects=stored_objects)
        assert trash(session_client, project, kept).status_code == status.HTTP_204_NO_CONTENT
        FileObject.all_objects.filter(pk=kept).update(deleted_at=timezone.now() - timedelta(days=31))

        # A 365 day project keeps a file the default window would have purged.
        assert purge_expired_files(batch_size=10) == {"purged": 0, "failed": 0, "scanned": 0}
        assert FileObject.all_objects.filter(pk=kept).exists() is True

        Project.objects.filter(pk=project.pk).update(retention_days=1)
        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=kept).exists() is False

    def test_the_batch_is_bounded_and_idempotent(self, session_client, project, stored_objects):
        file_ids = []
        for index in range(3):
            file_id, _ = upload_file(
                session_client, project, name=f"Batch{index}.pdf", stored_objects=stored_objects
            )
            assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
            file_ids.append(file_id)

        FileObject.all_objects.filter(pk__in=file_ids).update(deleted_at=timezone.now() - timedelta(days=31))

        first = purge_expired_files(batch_size=2)
        assert first == {"purged": 2, "failed": 0, "scanned": 2}
        assert FileObject.all_objects.filter(pk__in=file_ids).count() == 1

        second = purge_expired_batch(limit=10)
        assert second == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk__in=file_ids).count() == 0

        # Nothing left to do: a re-run is a no-op, not an error.
        assert purge_expired_files(batch_size=10) == {"purged": 0, "failed": 0, "scanned": 0}

    def test_the_purge_never_lists_the_bucket_or_touches_lifecycle_rules(
        self, session_client, project, stored_objects, monkeypatch, independent_store
    ):
        """AC-36 / AD-12 / AD-13: deletion is driven by the database, key by key."""
        file_object, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_object).status_code == status.HTTP_204_NO_CONTENT
        FileObject.all_objects.filter(pk=file_object).update(deleted_at=timezone.now() - timedelta(days=31))

        forbidden = {
            "list_objects",
            "list_objects_v2",
            "get_bucket_lifecycle_configuration",
            "put_bucket_lifecycle_configuration",
            "delete_bucket_lifecycle",
        }
        real_client = boto3.client

        class ForbiddenCallClient:
            """A real client that raises on the calls this feature must never make."""

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                if name in forbidden:
                    raise AssertionError(f"the purge must never call {name}")
                return getattr(self._inner, name)

        def client_factory(*args, **kwargs):
            return ForbiddenCallClient(real_client(*args, **kwargs))

        monkeypatch.setattr("plane.settings.storage.boto3.client", client_factory)

        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert object_exists(independent_store, object_key) is False
        assert FileObject.all_objects.filter(pk=file_object).exists() is False


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("second_operation", ["purge", "restore"])
def test_concurrent_purges_delete_and_account_once(
    session_client, project, stored_objects, monkeypatch, second_operation, create_user
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from django.db import close_old_connections
    from plane.utils.file_storage.purge import purge_file

    file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
    upload_file(session_client, project, name="Keep.pdf", stored_objects=stored_objects)
    assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
    snapshots = [FileObject.all_objects.select_related("project").get(pk=file_id) for _ in range(2)]
    entered, release, duplicate, second_started = Event(), Event(), Event(), Event()
    calls_lock = Lock()
    calls = []
    real_delete = S3Storage.delete_files

    def slow_delete(storage, keys):
        with calls_lock:
            calls.append(keys)
            if len(calls) > 1:
                duplicate.set()
        entered.set()
        assert release.wait(10)
        return real_delete(storage, keys)

    def run(snapshot, second=False):
        close_old_connections()
        try:
            if second:
                second_started.set()
                if second_operation == "restore":
                    client = APIClient()
                    client.force_authenticate(user=create_user)
                    return client.post(restore_url(project.workspace.slug, project.id, file_id)).status_code
            return purge_file(snapshot)
        finally:
            close_old_connections()

    with monkeypatch.context() as patcher:
        patcher.setattr(S3Storage, "delete_files", slow_delete)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, snapshots[0])
            try:
                assert entered.wait(5)
                second = pool.submit(run, snapshots[1], True)
                assert second_started.wait(5)
                assert not duplicate.wait(0.3)
            finally:
                release.set()
            assert first.result(timeout=10) is True
            result = second.result(timeout=10)
            assert result is True if second_operation == "purge" else result == 404
    assert len(calls) == 1
    assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.PURGED).count() == 1
    assert ProjectStorageUsage.objects.get(project=project).used_bytes == len(PDF_BYTES)
