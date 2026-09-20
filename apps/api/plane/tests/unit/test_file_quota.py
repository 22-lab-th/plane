# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Unit tests for quota reservation, settlement and single-fire release (§2.8)."""

# Python imports
from datetime import timedelta
from uuid import uuid4

# Django imports
from django.conf import settings
from django.utils import timezone

# Third party imports
import pytest

# Module imports
from plane.db.models import FileObject, FileVersion, Project, ProjectStorageUsage, StorageQuota, Workspace
from plane.utils.file_storage import quota


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Quota Workspace", slug="quota-workspace", owner=create_user)
    return Project.objects.create(name="Quota Project", identifier="QTA", workspace=workspace)


def make_version(project, version_no=1):
    file_object = FileObject.objects.create(
        project=project,
        name_original="spec.pdf",
        name_display="spec.pdf",
        name_normalized=f"spec-{uuid4()}.pdf",
        mime_type="application/pdf",
        bucket="plane-files-test",
        object_key=f"{uuid4()}.pdf",
        category=FileObject.Category.DOCS,
    )
    return FileVersion.objects.create(
        project=project,
        file=file_object,
        version_no=version_no,
        object_key=f"{uuid4()}.v{version_no}",
        bucket="plane-files-test",
        mime_type="application/pdf",
        reserved_bytes=0,
    )


@pytest.mark.unit
class TestReserve:
    """The ceiling test and both increments happen under the lock pair."""

    @pytest.mark.django_db
    def test_reserve_creates_rows_and_holds_capacity(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        expires_at = timezone.now() + timedelta(minutes=15)

        quota.reserve(quota_row, usage_row, version, requested_bytes=1024, expires_at=expires_at)

        version.refresh_from_db()
        quota_row.refresh_from_db()
        usage_row.refresh_from_db()

        assert version.reserved_bytes == 1024
        assert version.reservation_expires_at is not None
        assert quota_row.reserved_bytes == 1024
        assert usage_row.reserved_bytes == 1024
        assert quota_row.used_bytes == 0
        assert quota_row.limit_bytes == settings.PROJECT_FILE_WORKSPACE_QUOTA_BYTES

    @pytest.mark.django_db
    def test_reserve_is_a_no_op_when_limits_are_unlimited(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=None)
        quota_row.limit_bytes = None

        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=10**12, expires_at=timezone.now())

        version.refresh_from_db()
        assert version.reserved_bytes == 10**12

    @pytest.mark.django_db
    def test_workspace_ceiling_is_enforced(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=1000, used_bytes=900)
        quota_row.limit_bytes = 1000
        quota_row.used_bytes = 900

        with pytest.raises(quota.QuotaExceeded) as excinfo:
            quota.reserve(quota_row, usage_row, make_version(project), requested_bytes=200, expires_at=timezone.now())

        assert excinfo.value.code == "quota_exceeded"
        assert excinfo.value.level == "workspace"
        usage_row.refresh_from_db()
        assert usage_row.reserved_bytes == 0

    @pytest.mark.django_db
    def test_a_disabled_workspace_ceiling_is_not_enforced(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=10, enforce=False)
        quota_row.limit_bytes = 10
        quota_row.enforce = False

        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=500, expires_at=timezone.now())

        version.refresh_from_db()
        assert version.reserved_bytes == 500

    @pytest.mark.django_db
    def test_project_limit_is_enforced_independently(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=None)
        quota_row.limit_bytes = None
        ProjectStorageUsage.objects.filter(pk=usage_row.pk).update(limit_bytes=100)
        usage_row.limit_bytes = 100

        with pytest.raises(quota.QuotaExceeded) as excinfo:
            quota.reserve(quota_row, usage_row, make_version(project), requested_bytes=101, expires_at=timezone.now())

        assert excinfo.value.level == "project"


@pytest.mark.unit
class TestSettle:
    """Settlement converts a reservation into used bytes exactly once."""

    @pytest.mark.django_db
    def test_settle_activates_and_moves_the_counters(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=2048, expires_at=timezone.now())

        settled = quota.settle(quota_row, usage_row, version, observed_bytes=2000, activate=True)

        version.refresh_from_db()
        quota_row.refresh_from_db()
        usage_row.refresh_from_db()

        assert settled == FileVersion.Status.ACTIVE
        assert version.status == FileVersion.Status.ACTIVE
        assert version.is_active is True
        assert version.size_bytes == 2000
        assert version.reservation_released_at is not None
        assert quota_row.used_bytes == 2000
        assert quota_row.reserved_bytes == 0
        assert usage_row.used_bytes == 2000
        assert usage_row.reserved_bytes == 0

    @pytest.mark.django_db
    def test_settle_writes_the_evidence_it_is_given(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=10, expires_at=timezone.now())

        quota.settle(
            quota_row,
            usage_row,
            version,
            observed_bytes=10,
            activate=True,
            fields={"etag": '"abc"', "storage_metadata": {"magic_bytes": {"checked": True, "matches": True}}},
        )

        version.refresh_from_db()
        assert version.etag == '"abc"'
        assert version.storage_metadata["magic_bytes"] == {"checked": True, "matches": True}

    @pytest.mark.django_db
    def test_second_settle_changes_nothing(self, project):
        """AC-19/AC-40: a repeated settle matches zero rows and touches no counters."""
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=2048, expires_at=timezone.now())
        quota.settle(quota_row, usage_row, version, observed_bytes=2000, activate=True)

        assert quota.settle(quota_row, usage_row, version, observed_bytes=2000, activate=True) is None

        quota_row.refresh_from_db()
        usage_row.refresh_from_db()
        assert (quota_row.used_bytes, quota_row.reserved_bytes) == (2000, 0)
        assert (usage_row.used_bytes, usage_row.reserved_bytes) == (2000, 0)

    @pytest.mark.django_db
    def test_settle_fails_and_releases_when_the_object_exceeds_the_ceiling(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=1000)
        quota_row.limit_bytes = 1000
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=900, expires_at=timezone.now())

        with pytest.raises(quota.QuotaExceeded):
            quota.settle(quota_row, usage_row, version, observed_bytes=1500, activate=True)

        version.refresh_from_db()
        quota_row.refresh_from_db()
        usage_row.refresh_from_db()

        assert version.status == FileVersion.Status.FAILED
        assert version.is_active is False
        assert version.reservation_released_at is not None
        assert quota_row.reserved_bytes == 0
        assert quota_row.used_bytes == 0
        assert usage_row.reserved_bytes == 0


@pytest.mark.unit
class TestRelease:
    """The guarded release is single-fire, whoever gets there first."""

    @pytest.mark.django_db
    def test_release_decrements_both_rows_once(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=512, expires_at=timezone.now())

        assert quota.release(quota_row, usage_row, version) is True
        assert quota.release(quota_row, usage_row, version) is False

        quota_row.refresh_from_db()
        usage_row.refresh_from_db()
        assert quota_row.reserved_bytes == 0
        assert usage_row.reserved_bytes == 0

    @pytest.mark.django_db
    def test_release_is_a_no_op_after_settlement(self, project):
        quota_row, usage_row = quota.lock_usage_rows(project)
        version = make_version(project)
        quota.reserve(quota_row, usage_row, version, requested_bytes=512, expires_at=timezone.now())
        quota.settle(quota_row, usage_row, version, observed_bytes=500, activate=True)

        assert quota.release(quota_row, usage_row, version) is False

        quota_row.refresh_from_db()
        usage_row.refresh_from_db()
        assert quota_row.used_bytes == 500
        assert quota_row.reserved_bytes == 0
