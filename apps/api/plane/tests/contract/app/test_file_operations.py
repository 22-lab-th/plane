# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for file operations: rename, move and copy (AC-06, AC-10).

The copy assertions exist to kill one specific failure: a copy that reuses or
aliases the source's ``object_key`` would leave two rows pointing at one object,
so purging either file later deletes the other's bytes. Every test therefore
proves key independence, not just a successful response.
"""

# Python imports
import io
import uuid
from unittest import mock

# Django imports
from django.utils import timezone

# Third party imports
import pytest
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileAccessLog,
    FileCopyCleanup,
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

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"


def detail_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/"


def copy_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/copy/"


def folders_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/folders/"


def folder_url(slug, project_id, folder_id):
    return f"{folders_url(slug, project_id)}{folder_id}/"


def list_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


@pytest.fixture(autouse=True)
def storage_environment(monkeypatch):
    monkeypatch.setenv("MINIO_PUBLIC_ENDPOINT_URL", "http://test-minio:9000")
    monkeypatch.setenv("AWS_S3_ENDPOINT_URL", "http://test-minio:9000")


@pytest.fixture
def stored_objects():
    """Delete the objects a test stores (the database rolls back, the bucket does not)."""
    keys = []
    yield keys

    if keys:
        S3Storage().delete_files(keys)


@pytest.fixture
def project(create_user):
    workspace = Workspace.objects.create(name="Ops Workspace", slug="ops-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Ops Project", identifier="OPS", workspace=workspace)
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


def make_file(
    project,
    *,
    name="Report.pdf",
    folder=None,
    mime="application/pdf",
    content=PDF_BYTES,
    stored_objects,
    version_status=FileVersion.Status.ACTIVE,
    is_active=True,
    with_link=None,
):
    """Create a file with one stored version and a real object in MinIO."""
    object_key = (
        f"workspace/{project.workspace.slug}/projects/{project.ensure_storage_key()}"
        f"/docs/{uuid.uuid4()}/v1/{name}"
    )
    assert S3Storage().upload_file(io.BytesIO(content), object_key, content_type=mime) is True
    stored_objects.append(object_key)

    file_object = FileObject(
        project=project,
        folder=folder,
        name_original=name,
        name_display=name,
        name_normalized=normalize_name(name),
        mime_type=mime,
        extension=name.rpartition(".")[2],
        bucket="uploads",
        size_bytes=len(content),
        object_key=object_key,
        category=FileObject.Category.DOCS,
        status=FileObject.Status.ACTIVE,
        current_version_no=1,
    )
    file_object.save(force_insert=True, created_by_id=project.created_by_id)

    FileVersion.objects.create(
        project=project,
        file=file_object,
        version_no=1,
        object_key=object_key,
        bucket="uploads",
        mime_type=mime,
        size_bytes=len(content),
        status=version_status,
        is_active=is_active,
    )

    if with_link is not None:
        FileLink.objects.create(
            project=project,
            file=file_object,
            entity_type=FileLink.EntityType.ISSUE,
            entity_id=with_link.id,
            entity_identifier=f"OPS-{with_link.sequence_id}",
        )

    return file_object


def object_exists(key):
    return S3Storage().get_object_metadata(key) is not None


@pytest.mark.contract
@pytest.mark.django_db
class TestRenameAndMove:
    """AC-06: a rename is metadata-only and a move changes only the folder."""

    def test_rename_changes_no_key_and_touches_no_object(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Original.pdf", stored_objects=stored_objects)
        original_key = file_object.object_key
        original_name = file_object.name_original

        with mock.patch.object(S3Storage, "copy_object") as copy_object, mock.patch.object(
            S3Storage, "delete_files"
        ) as delete_files:
            response = session_client.patch(
                detail_url(project.workspace.slug, project.id, file_object.id),
                {"name_display": "Renamed.pdf"},
                format="json",
            )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["file"]["name_display"] == "Renamed.pdf"

        file_object.refresh_from_db()
        assert file_object.name_display == "Renamed.pdf"
        assert file_object.name_original == original_name
        assert file_object.object_key == original_key
        # No bytes moved: neither helper was called and the object is still there.
        copy_object.assert_not_called()
        delete_files.assert_not_called()
        assert object_exists(original_key)

        renamed = FileAccessLog.objects.filter(
            file_id=file_object.id, action=FileAccessLog.Action.RENAMED
        )
        assert renamed.count() == 1
        assert renamed.first().metadata["previous_name"] == "Original.pdf"

    def test_rename_to_a_taken_name_is_refused(self, session_client, project, stored_objects):
        folder = make_folder(project, "Docs")
        make_file(project, name="Taken.pdf", folder=folder, stored_objects=stored_objects)
        file_object = make_file(project, name="Mine.pdf", folder=folder, stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name_display": "taken.pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_name_conflict"
        file_object.refresh_from_db()
        assert file_object.name_display == "Mine.pdf"

    def test_move_changes_only_the_folder(self, session_client, project, stored_objects):
        target = make_folder(project, "Target")
        file_object = make_file(project, name="Movable.pdf", stored_objects=stored_objects)
        original_key = file_object.object_key

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"folder_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert str(response.data["file"]["folder_id"]) == str(target.id)

        file_object.refresh_from_db()
        assert file_object.folder_id == target.id
        assert file_object.object_key == original_key
        assert object_exists(original_key)
        assert FileAccessLog.objects.filter(
            file_id=file_object.id, action=FileAccessLog.Action.MOVED
        ).count() == 1

    def test_move_to_the_root_clears_the_folder(self, session_client, project, stored_objects):
        folder = make_folder(project, "Nested")
        file_object = make_file(project, name="Rooted.pdf", folder=folder, stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"folder_id": None},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        file_object.refresh_from_db()
        assert file_object.folder_id is None

    def test_move_to_a_deleted_folder_says_so(self, session_client, project, stored_objects):
        folder = make_folder(project, "Doomed")
        file_object = make_file(project, name="Homeless.pdf", stored_objects=stored_objects)
        session_client.delete(folder_url(project.workspace.slug, project.id, folder.id))

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"folder_id": str(folder.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "folder_trashed"

    def test_move_to_an_unknown_folder_says_so(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Homeless2.pdf", stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"folder_id": str(uuid.uuid4())},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "folder_not_found"

    def test_pinning_is_a_metadata_change(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Pinnable.pdf", stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"is_pinned": True},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        file_object.refresh_from_db()
        assert file_object.is_pinned is True

    def test_a_trashed_file_cannot_be_changed(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Trashed.pdf", stored_objects=stored_objects)
        FileObject.objects.filter(pk=file_object.pk).update(
            status=FileObject.Status.TRASHED, deleted_at=timezone.now()
        )

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name_display": "Nope.pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "file_trashed"

    def test_an_empty_body_is_rejected(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Empty.pdf", stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id), {}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_row_hidden_by_the_default_surface_cannot_be_changed(
        self, session_client, project, stored_objects
    ):
        """F-2: the write door applies the same visibility verdict as the read paths.

        A row whose ``deleted_at`` is set while its status is still ``active`` is
        hidden from the listing and 404s on detail for a MEMBER, so neither client
        may mutate it. An ADMIN may still *address* it on detail (the documented
        widener for the trash surface), which is addressing, not serving: the file
        stays undownloadable and unwritable for everyone.
        """
        member = add_member(project, email="hidden-member@example.com", role=15)
        member_client = client_for(member)
        file_object = make_file(project, name="Hidden.pdf", stored_objects=stored_objects)
        FileObject.all_objects.filter(pk=file_object.pk).update(deleted_at=timezone.now())

        url = detail_url(project.workspace.slug, project.id, file_object.id)
        listing = session_client.get(list_url(project.workspace.slug, project.id))
        assert str(file_object.id) not in [row["id"] for row in listing.data["results"]]
        assert member_client.get(url).status_code == status.HTTP_404_NOT_FOUND

        admin_view = session_client.get(url)
        assert admin_view.status_code == status.HTTP_200_OK
        assert admin_view.data["permissions"]["can_download"] is False

        for client in (session_client, member_client):
            rename = client.patch(url, {"name_display": "Nope.pdf"}, format="json")
            pinned = client.patch(url, {"is_pinned": True}, format="json")
            copy = client.post(copy_url(project.workspace.slug, project.id, file_object.id), {}, format="json")

            for response in (rename, pinned, copy):
                assert response.status_code == status.HTTP_404_NOT_FOUND
                assert response.data == {"error": "The required object does not exist."}

        file_object.refresh_from_db()
        assert file_object.name_display == "Hidden.pdf"
        assert file_object.is_pinned is False
        # Trash-inclusive: the row is soft-deleted, so the default manager hides it.
        assert FileObject.all_objects.filter(project=project).count() == 1

    def test_a_patch_with_an_unsupported_field_is_refused(self, session_client, project, stored_objects):
        """F-3: a field this endpoint does not implement is refused, not dropped."""
        file_object = make_file(project, name="Strict.pdf", stored_objects=stored_objects)
        target = make_folder(project, "Elsewhere")

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"target_project_id": str(uuid.uuid4()), "folder_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "unsupported_field"
        assert response.data["field"] == "target_project_id"
        file_object.refresh_from_db()
        # The supported field in the same payload is not applied either: the whole
        # request is rejected rather than half-honoured.
        assert file_object.folder_id is None
        assert file_object.name_display == "Strict.pdf"

    def test_a_patch_with_an_unknown_field_is_refused(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Strict2.pdf", stored_objects=stored_objects)

        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name": "typo.pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "unsupported_field"
        assert response.data["field"] == "name"
        file_object.refresh_from_db()
        assert file_object.name_display == "Strict2.pdf"

    def test_a_copy_with_an_unknown_field_is_refused(self, session_client, project, stored_objects):
        source = make_file(project, name="Strict3.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_url(project.workspace.slug, project.id, source.id), {"name": "typo.pdf"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "unsupported_field"
        assert response.data["field"] == "name"
        assert FileObject.objects.filter(project=project).count() == 1
        assert object_exists(source.object_key)

    def test_an_unknown_file_is_not_found(self, session_client, project):
        response = session_client.patch(
            detail_url(project.workspace.slug, project.id, uuid.uuid4()), {"name_display": "Nope.pdf"}, format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"error": "The required object does not exist."}


@pytest.mark.contract
@pytest.mark.django_db
class TestCopy:
    """AC-10 and the aliasing guard: a copy mints a new identity and new objects."""

    def test_copy_creates_a_new_file_with_its_own_key_and_objects(
        self, session_client, project, stored_objects
    ):
        source = make_file(project, name="Source.pdf", stored_objects=stored_objects)
        target = make_folder(project, "Copies")

        response = session_client.post(
            copy_url(project.workspace.slug, project.id, source.id),
            {"folder_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["source_file_id"] == str(source.id)
        assert response.data["copied_versions"] == 1

        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects.append(copy.object_key)

        assert copy.id != source.id
        assert copy.object_key != source.object_key
        # The new key is built from this project's prefix and the *new* file id.
        storage_key = Project.objects.get(pk=project.pk).storage_key
        assert f"/projects/{storage_key}/" in copy.object_key
        assert str(copy.id) in copy.object_key
        assert copy.category == source.category
        assert copy.folder_id == target.id
        assert copy.status == FileObject.Status.ACTIVE
        assert copy.current_version_no == 1

        # Both objects exist, and the copy carries the source's bytes.
        assert object_exists(source.object_key)
        assert object_exists(copy.object_key)
        assert S3Storage().get_object_metadata(copy.object_key)["ContentLength"] == len(PDF_BYTES)

        # No links are carried over.
        assert FileLink.objects.filter(file_id=copy.id).count() == 0

        # The target folder lists exactly the new row.
        listing = session_client.get(list_url(project.workspace.slug, project.id), {"folder_id": str(target.id)})
        assert [row["id"] for row in listing.data["results"]] == [str(copy.id)]

    def test_copy_is_not_an_alias_of_the_source_object(self, session_client, project, stored_objects):
        """Deleting the copy's object must not affect the source's bytes."""
        source = make_file(project, name="Independent.pdf", stored_objects=stored_objects)

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects.append(copy.object_key)

        assert S3Storage().delete_files([copy.object_key]) is True
        assert object_exists(copy.object_key) is False
        # The source is untouched: this is what an aliased key would break.
        assert object_exists(source.object_key) is True
        source_version = FileVersion.objects.get(file=source, version_no=1)
        assert object_exists(source_version.object_key) is True

    def test_copying_twice_mints_two_different_keys(self, session_client, project, stored_objects):
        source = make_file(project, name="Twice.pdf", stored_objects=stored_objects)

        first = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
        second = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")

        first_copy = FileObject.objects.get(id=first.data["file"]["id"])
        second_copy = FileObject.objects.get(id=second.data["file"]["id"])
        stored_objects += [first_copy.object_key, second_copy.object_key]

        assert len({first_copy.object_key, second_copy.object_key, source.object_key}) == 3
        for file_object in (first_copy, second_copy):
            assert len(FileVersion.objects.filter(file=file_object, object_key=file_object.object_key)) == 1

    def test_copy_carries_the_version_history(self, session_client, project, stored_objects):
        source = make_file(project, name="Versioned.pdf", stored_objects=stored_objects)
        second_key = f"{source.object_key}.v2"
        assert S3Storage().upload_file(io.BytesIO(b"%PDF-1.7\nsecond\n"), second_key, content_type="application/pdf")
        stored_objects.append(second_key)
        FileVersion.objects.create(
            project=project,
            file=source,
            version_no=2,
            object_key=second_key,
            bucket="uploads",
            mime_type="application/pdf",
            size_bytes=16,
            status=FileVersion.Status.SUPERSEDED,
            is_active=False,
        )

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")

        assert response.data["copied_versions"] == 2
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        copied_versions = list(FileVersion.objects.filter(file=copy).order_by("version_no"))
        stored_objects += [version.object_key for version in copied_versions]

        assert [version.version_no for version in copied_versions] == [1, 2]
        assert [version.is_active for version in copied_versions] == [True, False]
        source_keys = set(FileVersion.objects.filter(file=source).values_list("object_key", flat=True))
        copied_keys = {version.object_key for version in copied_versions}
        assert copied_keys.isdisjoint(source_keys)
        for version in copied_versions:
            assert object_exists(version.object_key)
            assert version.storage_metadata["copied_from"]["file_id"] == str(source.id)

    def test_copy_charges_this_project_for_the_copied_bytes(self, session_client, project, stored_objects):
        source = make_file(project, name="Charged.pdf", stored_objects=stored_objects)

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects.append(copy.object_key)

        usage = ProjectStorageUsage.objects.get(project=project)
        quota_row = StorageQuota.objects.get(workspace=project.workspace)
        assert usage.used_bytes == len(PDF_BYTES)
        assert usage.reserved_bytes == 0
        assert quota_row.used_bytes == len(PDF_BYTES)
        assert quota_row.reserved_bytes == 0
        assert response.data["storage_usage"]["project_used_bytes"] == len(PDF_BYTES)

    def test_copy_is_refused_when_the_quota_cannot_hold_it(self, session_client, project, stored_objects):
        source = make_file(project, name="TooBigToCopy.pdf", stored_objects=stored_objects)
        quota_row, _ = StorageQuota.objects.get_or_create(workspace=project.workspace)
        StorageQuota.objects.filter(pk=quota_row.pk).update(limit_bytes=1)

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "quota_exceeded"
        assert response.data["level"] == "workspace"
        assert isinstance(response.data["limit_bytes"], int)
        # Nothing was duplicated and nothing was charged. The counter rows were
        # materialised inside the refused transaction, so they may not exist at
        # all; either way they must not hold anything.
        assert FileObject.objects.filter(project=project).count() == 1
        usage = ProjectStorageUsage.objects.filter(project=project).first()
        assert usage is None or (usage.used_bytes, usage.reserved_bytes) == (0, 0)
        quota_after = StorageQuota.objects.filter(workspace=project.workspace).first()
        assert quota_after is None or (quota_after.used_bytes, quota_after.reserved_bytes) == (0, 0)
        # Nothing was duplicated: the project prefix still holds only the source's
        # object, so the refusal left no orphan bytes behind (the refusal happens
        # before a single copy, and the object copies sit behind the same lock).
        storage = S3Storage()
        prefix = f"workspace/{project.workspace.slug}/projects/{Project.objects.get(pk=project.pk).storage_key}/"
        listed = storage.s3_client.list_objects_v2(Bucket=storage.aws_storage_bucket_name, Prefix=prefix)
        assert [item["Key"] for item in listed.get("Contents", [])] == [source.object_key]

    def test_copy_refuses_the_cross_project_field_this_route_does_not_own(
        self, session_client, project, stored_objects
    ):
        """``copy/`` copies inside one project; another project is the T-122 route.

        The field is refused rather than dropped into an in-project copy (T-106 F-3):
        a client asking this door for another project must not read a 200 for an
        operation that did not happen.
        """
        other_project = Project.objects.create(name="Elsewhere", identifier="ELSE", workspace=project.workspace)
        source = make_file(project, name="StaysPut.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(other_project.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "unsupported_field"
        assert response.data["field"] == "target_project_id"
        assert FileObject.objects.filter(project=project).count() == 1
        assert FileObject.objects.filter(project=other_project).count() == 0
        assert object_exists(source.object_key)

    def test_copy_suffixes_a_taken_name(self, session_client, project, stored_objects):
        folder = make_folder(project, "Clash")
        make_file(project, name="Same.pdf", folder=folder, stored_objects=stored_objects)
        source = make_file(project, name="Elsewhere.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_url(project.workspace.slug, project.id, source.id),
            {"folder_id": str(folder.id), "name_display": "Same.pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects.append(copy.object_key)

        assert copy.name_display == "Same (2).pdf"
        assert copy.name_original == "Same (2).pdf"

    def test_copy_keeps_the_source_untouched(self, session_client, project, stored_objects):
        with_link = Issue.objects.create(
            name="Linked",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        source = make_file(project, name="Linked.pdf", stored_objects=stored_objects, with_link=with_link)

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects.append(copy.object_key)

        source.refresh_from_db()
        assert FileLink.objects.filter(file=source).count() == 1
        assert source.folder_id is None
        assert object_exists(source.object_key)

    def test_copy_of_a_superseded_only_file_is_refused(self, session_client, project, stored_objects):
        """F-1: with no active version there is nothing a copy could be a copy of."""
        source = make_file(
            project,
            name="SupersededOnly.pdf",
            stored_objects=stored_objects,
            version_status=FileVersion.Status.SUPERSEDED,
            is_active=False,
        )

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "object_unavailable"
        assert response.data["version_status"] is None
        assert FileObject.objects.filter(project=project).count() == 1
        assert object_exists(source.object_key)

    def test_copy_of_a_file_without_a_stored_version_is_refused(self, session_client, project, stored_objects):
        source = make_file(
            project,
            name="FailedOnly.pdf",
            stored_objects=stored_objects,
            version_status=FileVersion.Status.FAILED,
            is_active=False,
        )

        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["code"] == "object_unavailable"
        assert FileObject.objects.filter(project=project).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestOperationPermissions:
    """Guests may not mutate, non-members see nothing, archived is read-only."""

    def test_a_guest_cannot_rename_move_or_copy(self, project, stored_objects):
        guest = add_member(project, email="guest-ops@example.com", role=5)
        file_object = make_file(project, name="Guest.pdf", stored_objects=stored_objects)
        client = client_for(guest)

        rename = client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name_display": "Nope.pdf"},
            format="json",
        )
        move = client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id), {"folder_id": None}, format="json"
        )
        copy = client.post(copy_url(project.workspace.slug, project.id, file_object.id), {}, format="json")

        for response in (rename, move, copy):
            assert response.status_code == status.HTTP_403_FORBIDDEN

        file_object.refresh_from_db()
        assert file_object.name_display == "Guest.pdf"
        assert FileObject.objects.filter(project=project).count() == 1

    def test_a_non_member_gets_the_generic_404(self, project, stored_objects):
        outsider = add_member(project, email="outsider-ops@example.com", role=20, active=False)
        file_object = make_file(project, name="Private.pdf", stored_objects=stored_objects)
        client = client_for(outsider)

        rename = client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name_display": "Nope.pdf"},
            format="json",
        )
        copy = client.post(copy_url(project.workspace.slug, project.id, file_object.id), {}, format="json")

        assert rename.status_code == status.HTTP_404_NOT_FOUND
        assert rename.data == {"error": "The required object does not exist."}
        assert copy.status_code == status.HTTP_404_NOT_FOUND

    def test_an_archived_project_refuses_operations(self, session_client, project, stored_objects):
        file_object = make_file(project, name="Archived.pdf", stored_objects=stored_objects)
        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())

        rename = session_client.patch(
            detail_url(project.workspace.slug, project.id, file_object.id),
            {"name_display": "Nope.pdf"},
            format="json",
        )
        copy = session_client.post(copy_url(project.workspace.slug, project.id, file_object.id), {}, format="json")

        for response in (rename, copy):
            assert response.status_code == status.HTTP_409_CONFLICT
            assert response.data["code"] == "project_archived"

    def test_a_file_from_another_project_is_not_found(self, session_client, project, create_user, stored_objects):
        other_project = Project.objects.create(name="Other Ops", identifier="OOPS", workspace=project.workspace)
        ProjectMember.objects.create(
            project=other_project, member=create_user, workspace=project.workspace, role=20, is_active=True
        )
        foreign = make_file(other_project, name="Foreign.pdf", stored_objects=stored_objects)

        rename = session_client.patch(
            detail_url(project.workspace.slug, project.id, foreign.id), {"name_display": "Nope.pdf"}, format="json"
        )
        copy = session_client.post(copy_url(project.workspace.slug, project.id, foreign.id), {}, format="json")

        assert rename.status_code == status.HTTP_404_NOT_FOUND
        assert copy.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_timed_out_copy_keeps_durable_key_and_retries_cleanup(session_client, project, stored_objects):
    from datetime import timedelta
    from plane.utils.file_storage.copy_cleanup import cleanup_copy_keys

    source = make_file(project, name="Timeout.pdf", stored_objects=stored_objects)
    original_copy = S3Storage.copy_object
    attempted_keys = []

    def write_then_timeout(storage, source_key, target_key):
        # Ownership must already be committed before the remote write happens.
        assert FileCopyCleanup.objects.filter(object_key=target_key).exists()
        original_copy(storage, source_key, target_key)
        attempted_keys.append(target_key)
        stored_objects.append(target_key)
        raise TimeoutError("response lost after remote write")

    with mock.patch.object(S3Storage, "copy_object", write_then_timeout), mock.patch.object(
        S3Storage, "delete_files", return_value=False
    ):
        response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
    assert response.status_code == 502
    assert FileObject.objects.filter(project=project).count() == 1
    row = FileCopyCleanup.objects.get(object_key=attempted_keys[0])
    assert row.last_deleted_at is None
    assert object_exists(row.object_key)
    FileCopyCleanup.objects.filter(pk=row.pk).update(next_cleanup_at=timezone.now() - timedelta(seconds=1))
    assert cleanup_copy_keys()["deleted"] == 1
    assert not object_exists(row.object_key)
    assert object_exists(source.object_key)
    # A provider can finish late, even after an acknowledged delete. The key
    # remains journalled and the scheduled retry removes the resurrection.
    original_copy(S3Storage(), source.object_key, row.object_key)
    FileCopyCleanup.objects.filter(pk=row.pk).update(next_cleanup_at=timezone.now() - timedelta(seconds=1))
    assert cleanup_copy_keys()["deleted"] == 1
    assert not object_exists(row.object_key)
    assert FileCopyCleanup.objects.filter(pk=row.pk).exists()


@pytest.mark.contract
@pytest.mark.django_db(transaction=True)
def test_successful_copy_transfers_journal_ownership_to_version(session_client, project, stored_objects):
    source = make_file(project, name="Success.pdf", stored_objects=stored_objects)
    response = session_client.post(copy_url(project.workspace.slug, project.id, source.id), {}, format="json")
    assert response.status_code == 200
    copied = FileObject.objects.get(pk=response.data["file"]["id"])
    stored_objects.append(copied.object_key)
    assert not FileCopyCleanup.objects.exists()
    assert object_exists(copied.object_key)
