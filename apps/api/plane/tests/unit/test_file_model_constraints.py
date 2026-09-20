# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Database-level constraint tests for the project-file models (ARCH-001 §2).

These assert what the database enforces, not what the models declare: uniqueness
that PostgreSQL would otherwise let through (NULL columns in a unique index,
partial conditions) is exactly the class of defect these tests exist to catch.
"""

# Python imports
from uuid import uuid4

# Django imports
from django.db import IntegrityError, transaction
from django.utils import timezone

# Third party imports
import pytest

# Module imports
from plane.db.models import (
    FileFolder,
    FileJob,
    FileObject,
    FileVersion,
    Project,
    ProjectStorageUsage,
    StorageQuota,
    Workspace,
)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(
        name="File Constraints Workspace",
        slug="file-constraints-workspace",
        owner=create_user,
    )
    return Project.objects.create(name="File Constraints", identifier="FILEC", workspace=workspace)


def make_file(project, *, object_key=None, name_normalized="spec.pdf", folder=None, status=None):
    kwargs = {}
    if status is not None:
        kwargs["status"] = status

    return FileObject.objects.create(
        project=project,
        folder=folder,
        name_original="Spec.pdf",
        name_display="Spec.pdf",
        name_normalized=name_normalized,
        mime_type="application/pdf",
        extension="pdf",
        bucket="plane-files-test",
        object_key=object_key or f"{uuid4().hex}.pdf",
        category=FileObject.Category.DOCS,
        **kwargs,
    )


def make_version(project, file_object, version_no=1, *, object_key=None, is_active=False, status=None):
    kwargs = {}
    if status is not None:
        kwargs["status"] = status

    return FileVersion.objects.create(
        project=project,
        file=file_object,
        version_no=version_no,
        object_key=object_key or f"{uuid4().hex}.v{version_no}",
        bucket="plane-files-test",
        mime_type="application/pdf",
        is_active=is_active,
        **kwargs,
    )


@pytest.mark.unit
class TestFileObjectConstraints:
    """`file_objects`: key uniqueness and name uniqueness inside the project."""

    @pytest.mark.django_db
    def test_duplicate_object_key_is_rejected(self, project):
        object_key = f"workspace/ws/projects/p/docs/{uuid4()}/v1/spec.pdf"
        make_file(project, object_key=object_key)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                make_file(project, object_key=object_key)

    @pytest.mark.django_db
    def test_duplicate_name_at_the_project_root_is_rejected(self, project):
        """NULL folder_id must not defeat uniqueness at the root (PostgreSQL 15)."""
        make_file(project, name_normalized="spec.pdf")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                make_file(project, name_normalized="spec.pdf")

    @pytest.mark.django_db
    def test_same_name_in_different_folders_is_allowed(self, project):
        first_folder = FileFolder.objects.create(project=project, name="Specs", name_normalized="specs")
        second_folder = FileFolder.objects.create(project=project, name="Plans", name_normalized="plans")

        make_file(project, name_normalized="spec.pdf", folder=first_folder)
        make_file(project, name_normalized="spec.pdf", folder=second_folder)

        assert FileObject.objects.filter(project=project, name_normalized="spec.pdf").count() == 2

    @pytest.mark.django_db
    def test_trashed_file_does_not_block_a_live_one_of_the_same_name(self, project):
        make_file(project, name_normalized="spec.pdf", status=FileObject.Status.TRASHED)
        make_file(project, name_normalized="spec.pdf")

        assert FileObject.objects.filter(project=project, name_normalized="spec.pdf").count() == 2


@pytest.mark.unit
class TestFileFolderConstraints:
    """`file_folders`: one live folder name per parent, including the root."""

    @pytest.mark.django_db
    def test_duplicate_folder_name_at_the_root_is_rejected(self, project):
        """NULL parent_id must not defeat uniqueness at the root (PostgreSQL 15)."""
        FileFolder.objects.create(project=project, name="Specs", name_normalized="specs")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FileFolder.objects.create(project=project, name="Specs", name_normalized="specs")

    @pytest.mark.django_db
    def test_same_folder_name_under_different_parents_is_allowed(self, project):
        parent = FileFolder.objects.create(project=project, name="Parent", name_normalized="parent")

        FileFolder.objects.create(project=project, name="Specs", name_normalized="specs")
        FileFolder.objects.create(project=project, parent=parent, name="Specs", name_normalized="specs")

        assert FileFolder.objects.filter(project=project, name_normalized="specs").count() == 2


@pytest.mark.unit
class TestFileVersionConstraints:
    """`file_versions` names every stored object, so its keys are strictly unique."""

    @pytest.mark.django_db
    def test_duplicate_object_key_is_rejected(self, project):
        file_object = make_file(project)
        object_key = f"workspace/ws/projects/p/docs/{file_object.id}/v1/spec.pdf"
        make_version(project, file_object, object_key=object_key)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                make_version(project, file_object, version_no=2, object_key=object_key)

    @pytest.mark.django_db
    def test_duplicate_version_number_for_one_file_is_rejected(self, project):
        file_object = make_file(project)
        make_version(project, file_object, version_no=1)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                make_version(project, file_object, version_no=1)

    @pytest.mark.django_db
    def test_only_one_active_version_per_file(self, project):
        file_object = make_file(project)
        make_version(project, file_object, version_no=1, is_active=True, status=FileVersion.Status.ACTIVE)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                make_version(project, file_object, version_no=2, is_active=True, status=FileVersion.Status.ACTIVE)

        assert FileVersion.objects.filter(file=file_object, is_active=True).count() == 1

    @pytest.mark.django_db
    def test_superseded_version_coexists_with_the_active_one(self, project):
        file_object = make_file(project)
        make_version(project, file_object, version_no=1, is_active=True, status=FileVersion.Status.ACTIVE)
        make_version(project, file_object, version_no=2, status=FileVersion.Status.SUPERSEDED)

        assert FileVersion.objects.filter(file=file_object).count() == 2

    @pytest.mark.django_db
    def test_mark_status_moves_the_status_timestamp(self, project):
        """The cleanup sweep's age guard reads `status_changed_at` (ARCH-001 §2.4)."""
        file_object = make_file(project)
        version = make_version(project, file_object)
        created_stamp = version.status_changed_at

        version.mark_status(FileVersion.Status.FAILED)
        version.refresh_from_db()

        assert version.status == FileVersion.Status.FAILED
        assert version.status_changed_at > created_stamp
        assert version.status_changed_at <= timezone.now()


@pytest.mark.unit
class TestFileJobConstraints:
    """`file_jobs`: verification is unique per version, purge is unique per file."""

    @pytest.mark.django_db
    def test_only_one_verify_job_per_version(self, project):
        file_object = make_file(project)
        version = make_version(project, file_object)
        FileJob.objects.create(project=project, file=file_object, version=version, job_type=FileJob.JobType.VERIFY)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FileJob.objects.create(
                    project=project, file=file_object, version=version, job_type=FileJob.JobType.VERIFY
                )

    @pytest.mark.django_db
    def test_failed_thumbnail_job_can_be_queued_again(self, project):
        file_object = make_file(project)
        version = make_version(project, file_object)
        FileJob.objects.create(
            project=project,
            file=file_object,
            version=version,
            job_type=FileJob.JobType.THUMBNAIL,
            status=FileJob.Status.FAILED,
        )
        FileJob.objects.create(project=project, file=file_object, version=version, job_type=FileJob.JobType.THUMBNAIL)

        assert FileJob.objects.filter(version=version, job_type=FileJob.JobType.THUMBNAIL).count() == 2

    @pytest.mark.django_db
    def test_only_one_purge_job_per_file(self, project):
        file_object = make_file(project)
        FileJob.objects.create(project=project, file=file_object, job_type=FileJob.JobType.PURGE)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FileJob.objects.create(project=project, file=file_object, job_type=FileJob.JobType.PURGE)


@pytest.mark.unit
class TestQuotaModelConstraints:
    """One quota row per workspace and one usage row per project (§2.8, N-02a)."""

    @pytest.mark.django_db
    def test_one_storage_quota_per_workspace(self, project):
        StorageQuota.objects.create(workspace=project.workspace, limit_bytes=1024)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                StorageQuota.objects.create(workspace=project.workspace, limit_bytes=2048)

    @pytest.mark.django_db
    def test_one_usage_row_per_project(self, project):
        ProjectStorageUsage.objects.create(project=project, used_bytes=10)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ProjectStorageUsage.objects.create(project=project, used_bytes=20)
