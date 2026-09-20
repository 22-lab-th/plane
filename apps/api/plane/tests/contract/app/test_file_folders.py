# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for the folder tree (AC-09).

Create, rename, move and recursive delete, with the tree invariants the schema
cannot express: the depth limit, the no-cycle rule, depth recomputation for a
moved subtree, and a delete that trashes contents instead of unlinking them.
"""

# Python imports
import io
import uuid

# Django imports
from django.db import IntegrityError
from django.utils import timezone

# Third party imports
import pytest
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileObject,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.app.views.file.folders import NAME_CONSTRAINT_NAME, folder_conflict
from plane.settings.storage import S3Storage
from plane.utils.file_storage.naming import normalize_name

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"


def folders_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/folders/"


def folder_url(slug, project_id, folder_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/folders/{folder_id}/"


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
    workspace = Workspace.objects.create(name="Folders Workspace", slug="folders-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Folders Project", identifier="FOLD", workspace=workspace)
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


def make_folder(project, name, parent=None, depth=None):
    return FileFolder.objects.create(
        project=project,
        parent=parent,
        name=name,
        name_normalized=normalize_name(name),
        depth=depth if depth is not None else ((parent.depth + 1) if parent else 0),
    )


def make_file(project, folder, *, name="Report.pdf", stored_objects=None):
    object_key = f"workspace/{project.workspace.slug}/projects/FOLD/docs/{uuid.uuid4()}/v1/{name}"
    assert S3Storage().upload_file(io.BytesIO(PDF_BYTES), object_key, content_type="application/pdf") is True
    if stored_objects is not None:
        stored_objects.append(object_key)

    file_object = FileObject(
        project=project,
        folder=folder,
        name_original=name,
        name_display=name,
        name_normalized=normalize_name(name),
        mime_type="application/pdf",
        extension="pdf",
        bucket="uploads",
        size_bytes=len(PDF_BYTES),
        object_key=object_key,
        category=FileObject.Category.DOCS,
        status=FileObject.Status.ACTIVE,
        current_version_no=1,
    )
    file_object.save(force_insert=True, created_by_id=project.created_by_id)
    return file_object


@pytest.mark.contract
@pytest.mark.django_db
class TestFolderCreate:
    """Create at the root and nested, with the schema's uniqueness rules."""

    def test_create_at_root_sets_depth_zero(self, session_client, project):
        response = session_client.post(folders_url(project.workspace.slug, project.id), {"name": "Specs"}, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        folder = response.data["folder"]
        assert folder["name"] == "Specs"
        assert folder["parent_id"] is None
        assert folder["depth"] == 0
        assert [crumb["name"] for crumb in response.data["breadcrumbs"]] == ["Specs"]

    def test_create_nested_sets_depth_from_the_parent(self, session_client, project):
        parent = make_folder(project, "Specs")

        response = session_client.post(
            folders_url(project.workspace.slug, project.id),
            {"name": "Sub", "parent_id": str(parent.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["folder"]["depth"] == 1
        assert str(response.data["folder"]["parent_id"]) == str(parent.id)
        assert [crumb["name"] for crumb in response.data["breadcrumbs"]] == ["Specs", "Sub"]

    def test_duplicate_name_in_the_same_parent_is_refused(self, session_client, project):
        parent = make_folder(project, "Specs")
        make_folder(project, "Sub", parent=parent)

        response = session_client.post(
            folders_url(project.workspace.slug, project.id),
            {"name": "Sub", "parent_id": str(parent.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_name_conflict"
        assert FileFolder.objects.filter(parent=parent).count() == 1

    def test_duplicate_name_at_the_root_is_refused(self, session_client, project):
        """The NULL parent case: the unique index covers the root as well."""
        make_folder(project, "Specs")

        response = session_client.post(
            folders_url(project.workspace.slug, project.id), {"name": "specs"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_name_conflict"
        assert FileFolder.objects.filter(project=project, parent__isnull=True).count() == 1

    def test_case_only_difference_is_a_conflict(self, session_client, project):
        make_folder(project, "Docs")

        response = session_client.post(folders_url(project.workspace.slug, project.id), {"name": "DOCS"}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_a_parent_from_another_project_is_not_found(self, session_client, project):
        other_project = Project.objects.create(name="Other", identifier="OTHR", workspace=project.workspace)
        foreign = make_folder(other_project, "Foreign")

        response = session_client.post(
            folders_url(project.workspace.slug, project.id),
            {"name": "Mine", "parent_id": str(foreign.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_the_depth_limit_is_enforced(self, session_client, project):
        parent = make_folder(project, "Level 0")
        for level in range(1, 33):
            parent = make_folder(project, f"Level {level}", parent=parent, depth=level)

        assert parent.depth == 32

        response = session_client.post(
            folders_url(project.workspace.slug, project.id),
            {"name": "Too deep", "parent_id": str(parent.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "depth_limit_exceeded"
        assert response.data["max_depth"] == 32


@pytest.mark.contract
@pytest.mark.django_db
class TestFolderRenameAndMove:
    """Rename, move, depth recomputation and the no-cycle rule."""

    def test_rename_keeps_depth_and_updates_breadcrumbs(self, session_client, project):
        parent = make_folder(project, "Specs")
        folder = make_folder(project, "Sub", parent=parent)

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Renamed"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["folder"]["name"] == "Renamed"
        assert response.data["folder"]["depth"] == 1
        assert [crumb["name"] for crumb in response.data["breadcrumbs"]] == ["Specs", "Renamed"]

    def test_rename_to_a_taken_name_is_refused(self, session_client, project):
        make_folder(project, "Specs")
        folder = make_folder(project, "Plans")

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Specs"}, format="json"
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_name_conflict"

    def test_move_recomputes_depth_for_every_descendant(self, session_client, project):
        root_a = make_folder(project, "A")
        child = make_folder(project, "B", parent=root_a, depth=1)
        moved = make_folder(project, "C", parent=child, depth=2)
        leaf = make_folder(project, "D", parent=moved, depth=3)
        destination = make_folder(project, "E")

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, moved.id),
            {"parent_id": str(destination.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        moved.refresh_from_db()
        leaf.refresh_from_db()

        assert moved.parent_id == destination.id
        assert moved.depth == 1
        assert leaf.depth == 2
        assert [crumb["name"] for crumb in response.data["breadcrumbs"]] == ["E", "C"]

        # Moving the subtree to the root shifts it back down for every member.
        session_client.patch(
            folder_url(project.workspace.slug, project.id, moved.id),
            {"parent_id": None},
            format="json",
        )
        moved.refresh_from_db()
        leaf.refresh_from_db()

        assert moved.depth == 0
        assert leaf.depth == 1

    def test_move_into_own_descendant_is_refused(self, session_client, project):
        parent = make_folder(project, "Parent")
        child = make_folder(project, "Child", parent=parent, depth=1)
        grandchild = make_folder(project, "Grandchild", parent=child, depth=2)

        for target in (parent, child, grandchild):
            response = session_client.patch(
                folder_url(project.workspace.slug, project.id, parent.id),
                {"parent_id": str(target.id)},
                format="json",
            )

            assert response.status_code == status.HTTP_409_CONFLICT, target.name
            assert response.data["code"] == "folder_cycle"

        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.parent_id is None
        assert parent.depth == 0

    def test_move_that_would_breach_the_depth_limit_is_refused(self, session_client, project):
        moving = make_folder(project, "Moving")
        node = moving
        for level in range(1, 17):
            node = make_folder(project, f"Moving {level}", parent=node, depth=level)

        target_root = make_folder(project, "Target")
        target = target_root
        for level in range(1, 17):
            target = make_folder(project, f"Target {level}", parent=target, depth=level)

        # "Moving" sits at depth 0 with a leaf at 16; under the depth-16 target
        # its subtree would reach 33.
        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, moving.id),
            {"parent_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "depth_limit_exceeded"
        moving.refresh_from_db()
        assert moving.parent_id is None

    def test_breadcrumbs_for_a_four_level_path(self, session_client, project):
        first = make_folder(project, "One")
        second = make_folder(project, "Two", parent=first, depth=1)
        third = make_folder(project, "Three", parent=second, depth=2)
        fourth = make_folder(project, "Four", parent=third, depth=3)

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, fourth.id), {"name": "Four"}, format="json"
        )

        assert [crumb["name"] for crumb in response.data["breadcrumbs"]] == ["One", "Two", "Three", "Four"]
        assert [crumb["depth"] for crumb in response.data["breadcrumbs"]] == [0, 1, 2, 3]
        assert response.data["breadcrumbs"] == [
            {"id": str(first.id), "name": "One", "depth": 0},
            {"id": str(second.id), "name": "Two", "depth": 1},
            {"id": str(third.id), "name": "Three", "depth": 2},
            {"id": str(fourth.id), "name": "Four", "depth": 3},
        ]

    def test_a_cycle_in_the_data_does_not_spin_the_breadcrumb_walk(self, session_client, project):
        first = make_folder(project, "One")
        second = make_folder(project, "Two", parent=first, depth=1)
        # Corrupt the tree directly: the read path must still terminate.
        FileFolder.objects.filter(pk=first.pk).update(parent=second)

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, second.id), {"name": "Two"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK
        assert 1 <= len(response.data["breadcrumbs"]) <= 2


@pytest.mark.contract
@pytest.mark.django_db
class TestFolderDelete:
    """Recursive delete trashes contents; nothing is hard-deleted or unlinked."""

    def test_empty_folder_is_deleted_without_recursive(self, session_client, project):
        folder = make_folder(project, "Empty")

        response = session_client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert FileFolder.all_objects.get(pk=folder.pk).deleted_at is not None

    def test_non_empty_folder_without_recursive_is_refused(self, session_client, project, stored_objects):
        folder = make_folder(project, "Busy")
        make_folder(project, "Child", parent=folder, depth=1)
        existing = make_file(project, folder, name="kept.pdf", stored_objects=stored_objects)

        response = session_client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_not_empty"
        assert response.data["file_count"] == 1
        assert response.data["descendant_folder_count"] == 1

        # Nothing moved.
        existing.refresh_from_db()
        assert existing.status == FileObject.Status.ACTIVE
        assert existing.deleted_at is None
        assert FileFolder.objects.filter(pk=folder.pk).exists()

    def test_recursive_delete_trashes_files_and_nested_folders(
        self, session_client, project, stored_objects
    ):
        folder = make_folder(project, "Top")
        nested = make_folder(project, "Nested", parent=folder, depth=1)
        deeper = make_folder(project, "Deeper", parent=nested, depth=2)
        direct_file = make_file(project, folder, name="direct.pdf", stored_objects=stored_objects)
        deep_file = make_file(project, deeper, name="deep.pdf", stored_objects=stored_objects)

        response = session_client.delete(
            folder_url(project.workspace.slug, project.id, folder.id), {"recursive": True}
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        for file_object in (direct_file, deep_file):
            file_object.refresh_from_db()
            assert file_object.status == FileObject.Status.TRASHED
            assert file_object.deleted_at is not None
            # Never unlinked: the folder reference survives so restore can work.
            assert file_object.folder_id is not None
            # Never purged: the object is still in the bucket.
            assert S3Storage().get_object_metadata(file_object.object_key) is not None

        for nested_folder in (folder, nested, deeper):
            assert FileFolder.all_objects.get(pk=nested_folder.pk).deleted_at is not None
            assert FileFolder.objects.filter(pk=nested_folder.pk).exists() is False

        # One audit row per trashed file, and the files are gone from the listing.
        assert FileAccessLog.objects.filter(
            file_id=direct_file.id, action=FileAccessLog.Action.TRASHED
        ).count() == 1
        listing = session_client.get(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/", {"trashed": "true"}
        )
        assert {row["name_display"] for row in listing.data["results"]} == {"direct.pdf", "deep.pdf"}

    def test_recursive_delete_accepts_the_query_parameter(self, session_client, project, stored_objects):
        folder = make_folder(project, "Query")
        make_file(project, folder, name="file.pdf", stored_objects=stored_objects)

        response = session_client.delete(
            f"{folder_url(project.workspace.slug, project.id, folder.id)}?recursive=true"
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_a_garbage_recursive_flag_is_rejected(self, session_client, project, stored_objects):
        folder = make_folder(project, "Garbage")
        make_file(project, folder, name="file.pdf", stored_objects=stored_objects)

        response = session_client.delete(
            f"{folder_url(project.workspace.slug, project.id, folder.id)}?recursive=maybe"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"

    def test_an_upload_into_a_deleted_folder_says_so(self, session_client, project):
        folder = make_folder(project, "Gone")
        session_client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        response = session_client.post(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/initiate-upload/",
            {
                "file_name": "late.pdf",
                "size_bytes": 10,
                "mime_type": "application/pdf",
                "folder_id": str(folder.id),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_trashed"
        assert response.data["field"] == "folder_id"

    def test_an_upload_into_an_unknown_folder_says_so(self, session_client, project):
        response = session_client.post(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/initiate-upload/",
            {
                "file_name": "late.pdf",
                "size_bytes": 10,
                "mime_type": "application/pdf",
                "folder_id": str(uuid.uuid4()),
            },
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "folder_not_found"
        assert response.data["field"] == "folder_id"


@pytest.mark.unit
class TestFolderConflictMapping:
    """F-3: an integrity error is reported as the constraint it actually hit."""

    @staticmethod
    def _integrity_error(constraint_name):
        class _Diagnostics:
            pass

        class _Cause(Exception):
            pass

        cause = _Cause()
        cause.diag = _Diagnostics()
        cause.diag.constraint_name = constraint_name

        try:
            raise IntegrityError("boom") from cause
        except IntegrityError as exc:
            return exc

    def test_the_name_constraint_maps_to_a_name_conflict(self):
        error = folder_conflict(self._integrity_error(NAME_CONSTRAINT_NAME), name_normalized="specs")

        assert error.code == "folder_name_conflict"
        assert error.status_code == 409
        assert error.details["name_normalized"] == "specs"

    def test_a_depth_constraint_maps_to_the_depth_code(self):
        error = folder_conflict(self._integrity_error("file_folders_depth_check"), name_normalized="specs")

        assert error.code == "depth_limit_exceeded"
        assert error.status_code == 409

    def test_an_unidentified_constraint_maps_to_the_generic_code(self):
        error = folder_conflict(self._integrity_error("some_other_constraint"), name_normalized="specs")

        assert error.code == "folder_conflict"
        assert error.status_code == 409
        assert error.details["constraint"] == "some_other_constraint"

    def test_a_missing_constraint_name_is_still_reported(self):
        error = folder_conflict(IntegrityError("boom"), name_normalized="specs")

        assert error.code == "folder_conflict"


@pytest.mark.contract
@pytest.mark.django_db
class TestFolderPermissions:
    """Guests are read-only, non-members see nothing, archived is read-only."""

    def test_a_guest_cannot_mutate_folders(self, project):
        guest = add_member(project, email="guest-folders@example.com", role=5)
        folder = make_folder(project, "Shared")
        client = client_for(guest)

        create = client.post(folders_url(project.workspace.slug, project.id), {"name": "Nope"}, format="json")
        rename = client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Nope"}, format="json"
        )
        delete = client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        assert create.status_code == status.HTTP_403_FORBIDDEN
        assert rename.status_code == status.HTTP_403_FORBIDDEN
        assert delete.status_code == status.HTTP_403_FORBIDDEN
        assert FileFolder.objects.filter(pk=folder.pk).exists()
        assert FileFolder.objects.get(pk=folder.pk).name == "Shared"

    def test_a_non_member_gets_the_generic_404(self, project):
        outsider = add_member(project, email="outsider-folders@example.com", role=20, active=False)
        folder = make_folder(project, "Private")
        client = client_for(outsider)

        create = client.post(folders_url(project.workspace.slug, project.id), {"name": "Nope"}, format="json")
        rename = client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Nope"}, format="json"
        )

        assert create.status_code == status.HTTP_404_NOT_FOUND
        assert create.data == {"error": "The required object does not exist."}
        assert rename.status_code == status.HTTP_404_NOT_FOUND

    def test_an_archived_project_refuses_folder_mutations(self, session_client, project):
        folder = make_folder(project, "Archived")
        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())

        create = session_client.post(folders_url(project.workspace.slug, project.id), {"name": "Nope"}, format="json")
        rename = session_client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Nope"}, format="json"
        )
        delete = session_client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        for response in (create, rename, delete):
            assert response.status_code == status.HTTP_409_CONFLICT
            assert response.data["code"] == "project_archived"

    def test_a_deleted_project_is_not_found(self, session_client, project):
        folder = make_folder(project, "Deleted")
        Project.objects.filter(pk=project.pk).update(deleted_at=timezone.now())

        response = session_client.patch(
            folder_url(project.workspace.slug, project.id, folder.id), {"name": "Nope"}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
