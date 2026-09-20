# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Quota accounting, the usage API and reconciliation (AC-15, R-QUOTA-1/2).

The invariant every test here checks is one sentence: **the counters equal the sum
over the version rows, at both levels, and every reservation is released exactly
once.** The counters are never read back from a response alone - each assertion uses
a fresh query, because the T-102 F-5 finding was a stale in-memory counter - and
``reserved_bytes`` is always asserted equal to 0 rather than ``>= 0``.

The displayed pointer columns are deliberately never used to decide what is counted
(T-108 F-2): a file with ``current_version_no == 0`` still accounts for its stored
versions.
"""

# Python imports
import uuid
from unittest import mock

# Third party imports
import pytest
import requests
from botocore.exceptions import EndpointConnectionError
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.bgtasks.file_quota_task import reconcile_storage_batch, reconcile_storage_usage
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
from plane.utils.file_storage.quota import accounted_bytes, get_usage_rows

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
OTHER_BYTES = b"%PDF-1.7\nsecond payload\n%%EOF\n"


def files_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"{files_url(slug, project_id)}{file_id}/"


def storage_url(slug, project_id):
    return f"{files_url(slug, project_id)}storage/"


def upload_url(slug, project_id):
    return f"{files_url(slug, project_id)}initiate-upload/"


def complete_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}complete-upload/"


def abort_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}abort-upload/"


def copy_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}copy/"


def purge_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}purge/?confirm=true"


def links_url(slug, project_id, file_id):
    return f"{detail_url(slug, project_id, file_id)}links/"


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
    workspace = Workspace.objects.create(name="Quota Workspace", slug="quota-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Quota Project", identifier="QOTA", workspace=workspace)
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


def counters(project):
    """Both counter rows, read fresh - never from an in-memory instance."""
    return (
        ProjectStorageUsage.objects.get(project=project),
        StorageQuota.objects.get(workspace=project.workspace),
    )


def sum_stored_versions(project):
    """Σ over the versions the project's files still hold, from the rows alone."""
    return accounted_bytes(project)


def initiate(session_client, project, *, name="Report.pdf", size=len(PDF_BYTES), file_id=None):
    payload = {"file_name": name, "size_bytes": size, "mime_type": "application/pdf"}
    if file_id is not None:
        payload["file_id"] = str(file_id)
    return session_client.post(upload_url(project.workspace.slug, project.id), payload, format="json")


def complete(session_client, project, initiated, *, content=PDF_BYTES, declared=None, stored_objects=None):
    upload = initiated.data["upload"]
    assert requests.put(upload["url"], data=content, headers=upload["headers"], timeout=30).status_code == 200
    response = session_client.post(
        complete_url(project.workspace.slug, project.id, initiated.data["file"]["id"]),
        {
            "version_no": initiated.data["version_no"],
            "size_bytes": len(content) if declared is None else declared,
        },
        format="json",
    )
    if stored_objects is not None:
        key = FileVersion.objects.get(
            file_id=initiated.data["file"]["id"], version_no=initiated.data["version_no"]
        ).object_key
        stored_objects.append(key)
    return response


def upload_file(session_client, project, *, name="Report.pdf", content=PDF_BYTES, stored_objects):
    initiated = initiate(session_client, project, name=name, size=len(content))
    assert initiated.status_code == status.HTTP_200_OK, initiated.data
    completed = complete(session_client, project, initiated, content=content, stored_objects=stored_objects)
    assert completed.status_code == status.HTTP_200_OK, completed.data
    return initiated.data["file"]["id"]


@pytest.mark.contract
@pytest.mark.django_db
class TestStorageEndpoint:
    """AC-15 / R-QUOTA-1: the usage API answers with the counters, as integers."""

    def test_storage_reports_the_counters_and_the_ceiling_as_integers(
        self, session_client, project, stored_objects
    ):
        # The counter rows are materialised on first use, so configure them first:
        # an update against a row that does not exist yet would silently do nothing.
        get_usage_rows(project)
        StorageQuota.objects.filter(workspace=project.workspace).update(
            limit_bytes=10_000, warn_threshold_pct=75
        )
        file_id = upload_file(session_client, project, stored_objects=stored_objects)

        response = session_client.get(storage_url(project.workspace.slug, project.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        expected = {
            "project_used_bytes": len(PDF_BYTES),
            "workspace_used_bytes": len(PDF_BYTES),
            "limit_bytes": 10_000,
            "warn_threshold_pct": 75,
            "file_count": 1,
            "version_count": 1,
        }
        for field, value in expected.items():
            assert response.data[field] == value, f"{field}: {response.data} != {expected}"
        for field in ("project_used_bytes", "workspace_used_bytes", "limit_bytes", "warn_threshold_pct"):
            assert isinstance(response.data[field], int), f"{field}: {response.data}"

        # A project-level limit wins over the workspace ceiling (ARCH-001 §2.8).
        ProjectStorageUsage.objects.filter(project=project).update(limit_bytes=12_000)
        assert session_client.get(storage_url(project.workspace.slug, project.id)).data["limit_bytes"] == 12_000

        # And the numbers match a fresh read of the rows, not just each other.
        usage, quota = counters(project)
        assert usage.used_bytes == quota.used_bytes == len(PDF_BYTES)
        assert file_id  # the file exists, so the counts above are not vacuous

    def test_storage_reads_are_member_scoped(self, session_client, project):
        guest = add_member(project, email="quota-guest@example.com", role=5)
        outsider = add_member(project, email="quota-outsider@example.com", role=20, active=False)

        assert guest.get(storage_url(project.workspace.slug, project.id)).status_code == status.HTTP_200_OK
        assert (
            outsider.get(storage_url(project.workspace.slug, project.id)).status_code
            == status.HTTP_404_NOT_FOUND
        )
        assert (
            session_client.get(storage_url(project.workspace.slug, uuid.uuid4())).status_code
            == status.HTTP_404_NOT_FOUND
        )


@pytest.mark.contract
@pytest.mark.django_db
class TestEveryWriterMovesTheCountersExactlyOnce:
    """T-107/T-108/T-109 carry-forward (a): every writer, and `reserved_bytes == 0`."""

    def test_presign_reserves_and_finalize_settles(self, session_client, project, stored_objects):
        initiated = initiate(session_client, project, size=len(PDF_BYTES))
        usage, quota = counters(project)
        assert (usage.reserved_bytes, quota.reserved_bytes) == (len(PDF_BYTES), len(PDF_BYTES))
        assert (usage.used_bytes, quota.used_bytes) == (0, 0)

        completed = complete(session_client, project, initiated, stored_objects=stored_objects)
        assert completed.status_code == status.HTTP_200_OK, completed.data

        usage, quota = counters(project)
        assert usage.used_bytes == quota.used_bytes == len(PDF_BYTES)
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert sum_stored_versions(project) == len(PDF_BYTES)

    def test_abort_releases_the_reservation_exactly_once(self, session_client, project, stored_objects):
        initiated = initiate(session_client, project, size=len(PDF_BYTES))
        file_id = initiated.data["file"]["id"]

        first = session_client.post(
            abort_url(project.workspace.slug, project.id, file_id), {"version_no": 1}, format="json"
        )
        assert first.status_code == status.HTTP_204_NO_CONTENT, first.data

        usage, quota = counters(project)
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        version = FileVersion.objects.get(file_id=file_id, version_no=1)
        assert version.reservation_released_at is not None
        assert version.reserved_bytes == 0

        # A second abort is idempotent (204, the end state is the same) and cannot
        # decrement again: the release guard is the marker, not the caller.
        again = session_client.post(
            abort_url(project.workspace.slug, project.id, file_id), {"version_no": 1}, format="json"
        )
        usage, quota = counters(project)
        assert again.status_code == status.HTTP_204_NO_CONTENT
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert FileVersion.objects.filter(file_id=file_id).count() == 1

    def test_a_failed_finalize_releases_and_counts_nothing(
        self, session_client, project, stored_objects
    ):
        initiated = initiate(session_client, project, size=len(PDF_BYTES))
        failed = complete(
            session_client, project, initiated, declared=len(PDF_BYTES) + 5, stored_objects=stored_objects
        )
        assert failed.status_code == status.HTTP_400_BAD_REQUEST

        usage, quota = counters(project)
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert usage.used_bytes == quota.used_bytes == 0
        assert sum_stored_versions(project) == 0

    def test_a_copy_charges_this_project_for_the_copied_bytes(
        self, session_client, project, stored_objects
    ):
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        before = sum_stored_versions(project)

        copied = session_client.post(copy_url(project.workspace.slug, project.id, file_id), {}, format="json")
        assert copied.status_code == status.HTTP_200_OK, copied.data
        copy_id = copied.data["file"]["id"]
        stored_objects.append(FileVersion.objects.get(file_id=copy_id).object_key)

        usage, quota = counters(project)
        assert usage.used_bytes == quota.used_bytes == 2 * len(PDF_BYTES)
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert sum_stored_versions(project) == before + len(PDF_BYTES)
        assert usage.used_bytes == sum_stored_versions(project)

    def test_a_refused_copy_moves_nothing(self, session_client, project, stored_objects):
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        StorageQuota.objects.filter(workspace=project.workspace).update(limit_bytes=1)

        refused = session_client.post(copy_url(project.workspace.slug, project.id, file_id), {}, format="json")

        assert refused.status_code == status.HTTP_400_BAD_REQUEST
        assert refused.data["code"] == "quota_exceeded"
        assert isinstance(refused.data["limit_bytes"], int)
        usage, quota = counters(project)
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert usage.used_bytes == quota.used_bytes == len(PDF_BYTES)

    def test_a_whole_file_purge_gives_the_bytes_back_exactly_once(
        self, session_client, project, stored_objects
    ):
        issue = Issue.objects.create(
            name="Linked",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        assert (
            session_client.post(
                links_url(project.workspace.slug, project.id, file_id),
                {"entity_type": "issue", "entity_id": str(issue.id)},
                format="json",
            ).status_code
            == status.HTTP_200_OK
        )
        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        # Trash keeps the bytes counted (AD-09).
        assert counters(project)[0].used_bytes == len(PDF_BYTES)

        purged = session_client.delete(purge_url(project.workspace.slug, project.id, file_id))
        assert purged.status_code == status.HTTP_204_NO_CONTENT

        usage, quota = counters(project)
        assert usage.used_bytes == quota.used_bytes == 0
        assert usage.reserved_bytes == quota.reserved_bytes == 0
        assert sum_stored_versions(project) == 0

        # The purge's own record of the bytes it gave back is unaffected by the link
        # (carry-forward f): it is the sum over the file's versions, nothing else.
        audit = FileAccessLog.objects.get(file_id=file_id, action=FileAccessLog.Action.PURGED)
        assert audit.metadata["bytes"] == len(PDF_BYTES)

    def test_an_out_of_band_usage_counter_is_corrected_by_the_next_reconcile(
        self, session_client, project, stored_objects
    ):
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        ProjectStorageUsage.objects.filter(project=project).update(used_bytes=999_999)
        StorageQuota.objects.filter(workspace=project.workspace).update(used_bytes=999_999)

        summary = reconcile_storage_usage(batch_size=10)

        assert summary["projects_checked"] >= 1
        assert summary["projects_corrected"] == 1
        assert summary["corrections"][0]["project_id"] == str(project.id)
        assert summary["corrections"][0]["drift_bytes"] == 999_999 - len(PDF_BYTES)
        usage, quota = counters(project)
        assert usage.used_bytes == quota.used_bytes == len(PDF_BYTES)
        assert usage.used_bytes == sum_stored_versions(project)
        assert usage.file_count == 1 and usage.version_count == 1
        assert usage.recomputed_at is not None
        assert file_id


@pytest.mark.contract
@pytest.mark.django_db
class TestRecomputeReadsTheVersionRows:
    """Carry-forwards (b) and (c): the account of record is `file_versions`."""

    def test_a_partial_purge_keeps_every_version_counted_and_subtracts_nothing(
        self, session_client, project, stored_objects
    ):
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        revision = initiate(session_client, project, file_id=file_id)
        assert revision.data["version_no"] == 2
        assert complete(
            session_client, project, revision, content=OTHER_BYTES, stored_objects=stored_objects
        ).status_code == status.HTTP_200_OK
        total = len(PDF_BYTES) + len(OTHER_BYTES)
        assert counters(project)[0].used_bytes == total

        assert session_client.delete(detail_url(project.workspace.slug, project.id, file_id)).status_code == 204
        first_key = FileVersion.objects.get(file_id=file_id, version_no=1).object_key

        real_delete = S3Storage.delete_files
        failure = EndpointConnectionError(endpoint_url="http://test-minio:9000")

        def flaky_delete(self, object_names):
            if first_key in object_names:
                raise failure
            return real_delete(self, object_names)

        with mock.patch.object(S3Storage, "delete_files", flaky_delete):
            failed = session_client.delete(purge_url(project.workspace.slug, project.id, file_id))
        assert failed.status_code == status.HTTP_502_BAD_GATEWAY

        # The row survives, so every one of its versions still counts - including the
        # one whose object the attempt removed.
        primed = FileVersion.objects.get(file_id=file_id, version_no=2)
        assert primed.status == FileVersion.Status.PURGED
        assert primed.object_deleted_at is not None
        assert counters(project)[0].used_bytes == total
        assert sum_stored_versions(project) == total

        # A recompute agrees instead of "fixing" the numbers down.
        assert reconcile_storage_batch(batch_size=10)["projects_corrected"] == 0
        assert counters(project)[0].used_bytes == total

    def test_the_recompute_ignores_the_display_pointer(self, session_client, project, stored_objects):
        """Carry-forward (c): `current_version_no == 0` does not mean "nothing stored"."""
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        object_key = FileVersion.objects.get(file_id=file_id, version_no=1).object_key
        assert S3Storage().delete_files([object_key]) is True
        stored_objects.remove(object_key)

        # Strand the file: activating refuses and leaves it with no active version.
        assert (
            session_client.post(
                f"{detail_url(project.workspace.slug, project.id, file_id)}versions/1/activate/"
            ).status_code
            == status.HTTP_409_CONFLICT
        )
        stranded = FileObject.objects.get(pk=file_id)
        assert stranded.current_version_no == 0
        assert counters(project)[0].used_bytes == len(PDF_BYTES)

        summary = reconcile_storage_batch(batch_size=10)

        assert summary["projects_corrected"] == 0
        assert counters(project)[0].used_bytes == len(PDF_BYTES)
        assert sum_stored_versions(project) == len(PDF_BYTES)

    def test_activation_and_a_repair_move_no_bytes(self, session_client, project, stored_objects):
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        revision = initiate(session_client, project, file_id=file_id)
        assert complete(
            session_client, project, revision, content=OTHER_BYTES, stored_objects=stored_objects
        ).status_code == status.HTTP_200_OK
        before = counters(project)
        total = len(PDF_BYTES) + len(OTHER_BYTES)
        assert (before[0].used_bytes, before[1].used_bytes) == (total, total)

        activated = session_client.post(
            f"{detail_url(project.workspace.slug, project.id, file_id)}versions/2/activate/"
        )
        assert activated.status_code == status.HTTP_200_OK, activated.data

        after = counters(project)
        assert (after[0].used_bytes, after[1].used_bytes) == (total, total)
        assert (after[0].reserved_bytes, after[1].reserved_bytes) == (0, 0)


@pytest.mark.contract
@pytest.mark.django_db
class TestLinksMoveNoCounters:
    """Carry-forward (e): links are rows, not bytes."""

    def test_attach_unlink_and_relink_leave_the_counters_alone(
        self, session_client, project, stored_objects
    ):
        issue = Issue.objects.create(
            name="Linked",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        before = counters(project)

        attached = session_client.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
            format="json",
        )
        assert attached.status_code == status.HTTP_200_OK, attached.data
        link_id = attached.data["link"]["id"]
        assert session_client.delete(f"{links_url(project.workspace.slug, project.id, file_id)}{link_id}/").status_code == 204
        assert (
            session_client.post(
                links_url(project.workspace.slug, project.id, file_id),
                {"entity_type": "issue", "entity_id": str(issue.id)},
                format="json",
            ).status_code
            == status.HTTP_200_OK
        )

        after = counters(project)
        assert (after[0].used_bytes, after[1].used_bytes) == (before[0].used_bytes, before[1].used_bytes)
        assert (after[0].reserved_bytes, after[1].reserved_bytes) == (0, 0)
        assert after[0].used_bytes == sum_stored_versions(project)

    def test_the_entity_filter_does_not_change_the_usage_block(
        self, session_client, project, stored_objects
    ):
        issue = Issue.objects.create(
            name="Linked",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        file_id = upload_file(session_client, project, stored_objects=stored_objects)
        linked = session_client.post(
            links_url(project.workspace.slug, project.id, file_id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
            format="json",
        )
        assert linked.status_code == status.HTTP_200_OK
        unfiltered = session_client.get(files_url(project.workspace.slug, project.id))
        filtered = session_client.get(
            files_url(project.workspace.slug, project.id),
            {"entity_type": "issue", "entity_id": str(issue.id)},
        )

        assert filtered.data["storage"] == unfiltered.data["storage"]
        assert unfiltered.data["storage"]["project_used_bytes"] == len(PDF_BYTES)


@pytest.mark.contract
@pytest.mark.django_db
class TestReconciliation:
    """R-QUOTA-1's self-correcting guarantee and §2.8 item 7's bucket comparison."""

    def test_reserved_bytes_are_recomputed_from_the_rows(self, session_client, project):
        initiated = initiate(session_client, project, size=len(PDF_BYTES))
        assert initiated.status_code == status.HTTP_200_OK
        # Corrupt the reservation counter on both rows.
        ProjectStorageUsage.objects.filter(project=project).update(reserved_bytes=42)
        StorageQuota.objects.filter(workspace=project.workspace).update(reserved_bytes=42)

        summary = reconcile_storage_batch(batch_size=10)

        assert summary["projects_corrected"] == 1
        usage, quota = counters(project)
        assert usage.reserved_bytes == len(PDF_BYTES)
        assert usage.used_bytes == 0

    def test_reconciliation_is_idempotent_and_batch_bound(self, session_client, project, stored_objects):
        for _ in range(3):
            upload_file(session_client, project, name=f"File-{uuid.uuid4().hex[:6]}.pdf", stored_objects=stored_objects)

        first = reconcile_storage_batch(batch_size=10)
        second = reconcile_storage_batch(batch_size=10)

        assert first["projects_corrected"] == 0
        assert second["projects_corrected"] == 0
        assert second["accounted_bytes"] == first["accounted_bytes"]
        assert counters(project)[0].used_bytes == 3 * len(PDF_BYTES)
        # The workspace row is the sum of its projects.
        assert counters(project)[1].used_bytes == counters(project)[0].used_bytes

    def test_the_bucket_comparison_alerts_only_beyond_the_tolerance(
        self, session_client, project, stored_objects
    ):
        upload_file(session_client, project, stored_objects=stored_objects)
        accounted = len(PDF_BYTES)

        with mock.patch.object(S3Storage, "get_bucket_usage_bytes", return_value=None):
            unavailable = reconcile_storage_batch(batch_size=10)
        assert unavailable["bucket_usage_available"] is False
        assert unavailable["bucket_bytes"] is None
        assert unavailable["alert"] is False

        with mock.patch.object(S3Storage, "get_bucket_usage_bytes", return_value=accounted):
            clean = reconcile_storage_batch(batch_size=10)
        assert clean["alert"] is False
        assert clean["unaccounted_bytes"] == 0

        tolerance = clean["tolerance_bytes"]
        assert tolerance >= 1
        with mock.patch.object(S3Storage, "get_bucket_usage_bytes", return_value=accounted + tolerance + 1):
            drifting = reconcile_storage_batch(batch_size=10)
        assert drifting["alert"] is True
        assert drifting["unaccounted_bytes"] == tolerance + 1
        assert drifting["accounted_bytes"] == accounted

    def test_the_workspace_row_follows_its_projects(self, session_client, project, stored_objects):
        upload_file(session_client, project, stored_objects=stored_objects)
        StorageQuota.objects.filter(workspace=project.workspace).update(used_bytes=123_456)

        summary = reconcile_storage_batch(batch_size=10)

        quota = StorageQuota.objects.get(workspace=project.workspace)
        assert quota.used_bytes == len(PDF_BYTES)
        assert summary["accounted_bytes"] == len(PDF_BYTES)
