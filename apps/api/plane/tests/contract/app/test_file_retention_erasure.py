# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Retention configuration and the erasure procedure (AC-26, AC-27, R-LEG-1, R-LEG-2).

Two claims are asserted here rather than assumed:

* **the retention period is the project's own column, honoured end to end** - set and
  read back through the project API, refused when it cannot be honoured, and consumed by
  the purge and by the restore refusal at the same instant (a file is restorable exactly
  while it is not yet purgeable);
* **an erasure leaves a provable record** - every version object and every link row of
  the requested file is gone, and the ``purged`` audit row that survives the file carries
  the actor, the timestamp and the exact keys, which is what a completion confirmation
  cites 90 days later.

Object claims are made with an independent boto3 client and the retention task is run as
the real function (never ``.delay()``: the queue is not part of this environment).
"""

# Python imports
from datetime import timedelta
from unittest import mock

# Django imports
from django.conf import settings
from django.utils import timezone

# Third party imports
import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from rest_framework import status

# Module imports
from plane.bgtasks.file_purge_task import purge_expired_files
from plane.db.models import (
    FileAccessLog,
    FileLink,
    FileObject,
    FileVersion,
    Issue,
    Project,
    ProjectMember,
    ProjectStorageUsage,
    State,
    StorageQuota,
    Workspace,
    WorkspaceMember,
)
from plane.settings.storage import S3Storage
from plane.utils.file_storage.purge import purgeable_files

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def activity_url(slug, project_id):
    return f"{files_url(slug, project_id)}activity/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}complete-upload/"


def links_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}links/"


def link_url(slug, project_id, file_id, link_id):
    return f"{links_url(slug, project_id, file_id)}{link_id}/"


def restore_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}restore/"


def purge_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}purge/?confirm=true"


def project_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/"


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
    try:
        store.head_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


@pytest.fixture
def stored_objects():
    """Delete whatever objects a test stored; the database rolls back, MinIO does not."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project_api_without_the_broker(monkeypatch):
    """Stub the two enqueues the project API makes after a write.

    DEFECT-010: the queue is not resolvable in this environment, so ``model_activity.delay``
    on the post-save path of ``partial_update`` (and ``recent_visited_task.delay`` on
    ``retrieve``) raises and the response is a 500 while the row is already written. The
    enqueue is not what this suite asserts, so it is stubbed rather than left to the
    broker's health. Nothing else here calls ``.delay()``: the file module deliberately
    uses queryset updates, which is why the file endpoints work without this fixture.
    """
    monkeypatch.setattr("plane.app.views.project.base.model_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.app.views.project.base.recent_visited_task.delay", lambda **kwargs: None)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Retention Workspace", slug="retention-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Retention Project", identifier="RTN", workspace=workspace)
    # ADMIN: the purge door is admin-only, and this suite is about the window and the
    # erasure rather than about the roles that T-107's suite already covers.
    ProjectMember.objects.create(
        project=project, member=create_user, workspace=workspace, role=20, is_active=True
    )
    return project


def set_retention(session_client, project, value):
    """Set the per-project retention period through the API, as the settings UI does."""
    return session_client.patch(
        project_url(project.workspace.slug, project.id), {"retention_days": value}, format="json"
    )


def make_state(project):
    return State.objects.create(
        name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
    )


def upload_file(session_client, project, *, name="Report.pdf", stored_objects, link_to=None):
    """Create one verified version through the real pipeline and return ``(file_id, key)``."""
    payload = {"file_name": name, "size_bytes": len(PDF_BYTES), "mime_type": "application/pdf"}
    if link_to is not None:
        payload["link"] = {"entity_type": link_to[0], "entity_id": str(link_to[1])}

    initiated = session_client.post(upload_url(project.workspace.slug, project.id), payload, format="json")
    assert initiated.status_code == status.HTTP_200_OK, initiated.data

    file_id = initiated.data["file"]["id"]
    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=PDF_BYTES, headers=upload["headers"], timeout=30).status_code == 200

    completed = session_client.post(
        complete_url(project.workspace.slug, project.id, file_id),
        {"version_no": 1, "size_bytes": len(PDF_BYTES)},
        format="json",
    )
    assert completed.status_code == status.HTTP_200_OK, completed.data

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

    object_key = FileVersion.objects.get(file_id=file_id, version_no=2).object_key
    stored_objects.append(object_key)
    return object_key


def trash(session_client, project, file_id):
    return session_client.delete(detail_url(project.workspace.slug, project.id, file_id))


def backdate_trash(project, file_id, *, days):
    """Move a trashed file's window start back, as time passing would."""
    FileObject.all_objects.filter(pk=file_id).update(deleted_at=timezone.now() - timedelta(days=days))


def usage_bytes(project):
    return (
        ProjectStorageUsage.objects.get(project=project).used_bytes,
        StorageQuota.objects.get(workspace=project.workspace).used_bytes,
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestRetentionConfiguration:
    """AC-26 / R-LEG-1: configurable, stored and returned by the project API."""

    def test_the_period_is_stored_and_returned_by_the_project_api(
        self, session_client, project, project_api_without_the_broker
    ):
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK

        assert Project.objects.get(pk=project.pk).retention_days == 7
        detail = session_client.get(project_url(project.workspace.slug, project.id))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["retention_days"] == 7

        # And it can be cleared again: null is the deployment default, not "no retention".
        assert set_retention(session_client, project, None).status_code == status.HTTP_200_OK
        assert Project.objects.get(pk=project.pk).retention_days is None
        assert session_client.get(project_url(project.workspace.slug, project.id)).data["retention_days"] is None

    def test_a_period_the_purge_cannot_honour_is_refused_and_changes_nothing(
        self, session_client, project, project_api_without_the_broker
    ):
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK

        for refused_value in (0, -3, "seven"):
            refused = set_retention(session_client, project, refused_value)
            assert refused.status_code == status.HTTP_400_BAD_REQUEST, refused.data
            assert "retention_days" in refused.data

        # Nothing was stored: a zero would be read as "not set" by the purge and would
        # silently hand the project the 30-day default.
        assert Project.objects.get(pk=project.pk).retention_days == 7

    def test_a_project_without_a_period_purges_on_the_deployment_default(
        self, session_client, project, stored_objects, independent_store
    ):
        window = settings.PROJECT_FILE_TRASH_DAYS
        expired, expired_key = upload_file(session_client, project, name="Old.pdf", stored_objects=stored_objects)
        fresh, fresh_key = upload_file(session_client, project, name="New.pdf", stored_objects=stored_objects)
        for file_id in (expired, fresh):
            assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT

        backdate_trash(project, expired, days=window + 1)
        backdate_trash(project, fresh, days=window - 1)

        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert FileObject.all_objects.filter(pk=expired).exists() is False
        assert object_exists(independent_store, expired_key) is False
        assert FileObject.all_objects.filter(pk=fresh).exists() is True
        assert object_exists(independent_store, fresh_key) is True

    def test_a_period_set_through_the_api_drives_the_purge_window(
        self, session_client, project, project_api_without_the_broker, stored_objects, independent_store
    ):
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK
        expired, expired_key = upload_file(session_client, project, name="Old.pdf", stored_objects=stored_objects)
        fresh, fresh_key = upload_file(session_client, project, name="New.pdf", stored_objects=stored_objects)
        for file_id in (expired, fresh):
            assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT

        backdate_trash(project, expired, days=8)
        backdate_trash(project, fresh, days=6)

        # The 30-day default would have purged neither of these; the project's own
        # 7-day period purges the older one and leaves the younger one alone.
        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert object_exists(independent_store, expired_key) is False
        assert FileObject.all_objects.filter(pk=fresh).exists() is True
        assert object_exists(independent_store, fresh_key) is True
        assert session_client.post(restore_url(project.workspace.slug, project.id, fresh)).status_code == (
            status.HTTP_200_OK
        )

    def test_a_longer_period_holds_a_file_the_default_would_have_purged(
        self, session_client, project, project_api_without_the_broker, stored_objects, independent_store
    ):
        assert set_retention(session_client, project, 365).status_code == status.HTTP_200_OK
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        backdate_trash(project, file_id, days=settings.PROJECT_FILE_TRASH_DAYS + 1)

        assert purge_expired_files(batch_size=10) == {"purged": 0, "failed": 0, "scanned": 0}
        assert FileObject.all_objects.filter(pk=file_id).exists() is True
        assert object_exists(independent_store, object_key) is True

        # Lowering the period through the API hands the same file to the next run.
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK
        assert purge_expired_files(batch_size=10) == {"purged": 1, "failed": 0, "scanned": 1}
        assert object_exists(independent_store, object_key) is False


@pytest.mark.contract
@pytest.mark.django_db
class TestRestoreAgainstTheWindow:
    """R-DEL-2's edge case / R-LIFE-1: the purge wins once the window has elapsed."""

    def test_a_file_past_its_window_cannot_be_restored(
        self, session_client, project, project_api_without_the_broker, stored_objects, independent_store
    ):
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK
        file_id, object_key = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        backdate_trash(project, file_id, days=8)

        refused = session_client.post(restore_url(project.workspace.slug, project.id, file_id))

        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "retention_expired"
        assert refused.data["retention_days"] == 7
        # Nothing moved: the row is still trashed, its bytes are still stored, and no
        # restore was recorded.
        stored = FileObject.all_objects.get(pk=file_id)
        assert stored.status == FileObject.Status.TRASHED
        assert stored.deleted_at is not None
        assert object_exists(independent_store, object_key) is True
        assert FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.RESTORED).count() == 0

    def test_the_window_boundary_belongs_to_the_purge(
        self, session_client, project, project_api_without_the_broker, stored_objects
    ):
        """Exactly at the window, the file the purge selects is the file restore refuses."""
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        FileObject.all_objects.filter(pk=file_id).update(deleted_at=timezone.now() - timedelta(days=7))

        assert file_id in [str(row.id) for row in purgeable_files()]
        refused = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "retention_expired"

    def test_a_file_inside_its_window_is_still_restorable_and_not_purgeable(
        self, session_client, project, project_api_without_the_broker, stored_objects
    ):
        assert set_retention(session_client, project, 7).status_code == status.HTTP_200_OK
        file_id, _ = upload_file(session_client, project, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        backdate_trash(project, file_id, days=7 - 1 / 24)

        assert file_id not in [str(row.id) for row in purgeable_files()]
        restored = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert restored.status_code == status.HTTP_200_OK
        assert FileObject.all_objects.get(pk=file_id).status != FileObject.Status.TRASHED

    def test_a_partial_purge_inside_the_window_still_restores_for_repair(
        self, session_client, project, stored_objects, independent_store
    ):
        """A purge that failed part way is restorable while the window still runs.

        The delivery invariant is carried by ``can_download`` and by activation
        refusing a version whose bytes are gone (T-108 carry-forward 3), not by the
        status: refusing this row would strand a partially purged file that the
        worker can no longer repair.
        """
        file_id, first_key = upload_file(session_client, project, stored_objects=stored_objects)
        second_key = add_revision(session_client, project, file_id, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT

        real_delete = S3Storage.delete_files
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        def flaky_delete(self, object_names):
            if first_key in object_names:
                raise failure
            return real_delete(self, object_names)

        with mock.patch.object(S3Storage, "delete_files", flaky_delete):
            failed = session_client.delete(purge_url(project.workspace.slug, project.id, file_id))

        assert failed.status_code == status.HTTP_502_BAD_GATEWAY
        assert FileObject.all_objects.get(pk=file_id).status == FileObject.Status.PURGE_FAILED
        assert object_exists(independent_store, second_key) is False, "the attempt removed what it could"

        # Inside the window: restorable, and the repair path is what decides servability.
        restored = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert restored.status_code == status.HTTP_200_OK, restored.data
        assert FileObject.all_objects.get(pk=file_id).status != FileObject.Status.PURGE_FAILED

        # And the window still binds: pushed past it, the same row is refused.
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        backdate_trash(project, file_id, days=settings.PROJECT_FILE_TRASH_DAYS + 1)
        refused = session_client.post(restore_url(project.workspace.slug, project.id, file_id))
        assert refused.status_code == status.HTTP_409_CONFLICT
        assert refused.data["code"] == "retention_expired"


@pytest.mark.contract
@pytest.mark.django_db
class TestErasureProcedure:
    """AC-27 / R-LEG-2: every version and link goes, and the record of it stays."""

    def test_an_erasure_removes_every_version_and_link_and_records_completion(
        self, session_client, project, create_user, stored_objects, independent_store
    ):
        state = make_state(project)
        issue = Issue.objects.create(
            name="Attached", project=project, workspace=project.workspace, state=state
        )
        second_issue = Issue.objects.create(
            name="Embedding", project=project, workspace=project.workspace, state=state
        )
        file_id, first_key = upload_file(
            session_client, project, stored_objects=stored_objects, link_to=("issue", issue.id)
        )
        second_key = add_revision(session_client, project, file_id, stored_objects=stored_objects)

        # A second link, then revoke it: an inactive link row is still a link row the
        # erasure has to remove.
        attached = session_client.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(second_issue.id)},
            format="json",
        )
        assert attached.status_code == status.HTTP_200_OK, attached.data
        assert (
            session_client.delete(
                link_url(project.workspace.slug, project.id, file_id, attached.data["link"]["id"])
            ).status_code
            == status.HTTP_204_NO_CONTENT
        )
        assert FileLink.all_objects.filter(file_id=file_id).count() == 2
        assert FileLink.objects.filter(file_id=file_id).count() == 1

        # The documented procedure for a live file: trash, then purge.
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        purge = session_client.delete(purge_url(project.workspace.slug, project.id, file_id))

        assert purge.status_code == status.HTTP_204_NO_CONTENT
        # Objects first: this assertion has to come before the row assertions.
        for key in (first_key, second_key):
            assert object_exists(independent_store, key) is False, key

        assert FileObject.all_objects.filter(pk=file_id).exists() is False
        assert FileVersion.objects.filter(file_id=file_id).count() == 0
        assert FileLink.all_objects.filter(file_id=file_id).count() == 0
        assert usage_bytes(project) == (0, 0)

        completion = FileAccessLog.objects.filter(file_id=file_id, action=FileAccessLog.Action.PURGED)
        assert completion.count() == 1
        record = completion.get()
        assert record.actor_id == create_user.id
        assert record.created_at is not None
        assert record.metadata["trigger"] == "manual"
        assert record.metadata["versions"] == 2
        assert record.metadata["bytes"] == 2 * len(PDF_BYTES)
        assert set(record.metadata["object_keys"]) == {first_key, second_key}

    def test_the_completion_record_outlives_the_file_and_is_readable(
        self, session_client, project, create_user, stored_objects, independent_store
    ):
        file_id, first_key = upload_file(session_client, project, name="Erase-me.pdf", stored_objects=stored_objects)
        object_key = add_revision(session_client, project, file_id, stored_objects=stored_objects)
        assert trash(session_client, project, file_id).status_code == status.HTTP_204_NO_CONTENT
        assert (
            session_client.delete(purge_url(project.workspace.slug, project.id, file_id)).status_code
            == status.HTTP_204_NO_CONTENT
        )

        # The file is gone from every door...
        assert session_client.get(detail_url(project.workspace.slug, project.id, file_id)).status_code == (
            status.HTTP_404_NOT_FOUND
        )
        # ...and the record the confirmation cites is what remains.
        trail = session_client.get(
            activity_url(project.workspace.slug, project.id), {"file_id": str(file_id), "action": "purged"}
        )

        assert trail.status_code == status.HTTP_200_OK
        assert trail.data["page"]["total_results"] == 1
        row = trail.data["results"][0]
        assert row["file_id"] == str(file_id)
        assert row["actor"]["id"] == str(create_user.id)
        assert row["created_at"] is not None
        assert row["file_name_snapshot"] == "Erase-me.pdf"
        assert set(row["metadata"]["object_keys"]) == {first_key, object_key}
