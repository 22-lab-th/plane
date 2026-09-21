# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for cross-project copy and move (AC-41, AC-42, AD-17).

Three failure shapes are what these tests exist to kill, none of which a status code
alone would catch:

* a copy that keeps pointing at the source's bytes, or that charges the source's quota
  instead of the target's - every test asserts key independence, the target's prefix and
  both projects' counters (the source project is charged for its file first, so the
  counters have something to move rather than something to invent);
* an operation that skips one side's permission check, or that crosses a workspace
  boundary;
* a **move** that deletes the source before the copy verified, or that leaves two copies
  behind. The verification-failure test drives the real HEAD check against a corrupted
  store response and then asserts the source is byte-for-byte untouched *and* that the
  target kept nothing at all.
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
REVISION_BYTES = b"%PDF-1.7\nsecond revision\n"


def copy_to_project_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/copy-to-project/"


def move_to_project_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/move-to-project/"


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
    workspace = Workspace.objects.create(name="Cross Workspace", slug="cross-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    project = Project.objects.create(name="Source Project", identifier="SRC", workspace=workspace)
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


def make_target(workspace, create_user, *, name="Target Project", identifier="TGT", role=20, archived=False):
    """A second project of the same workspace, with the caller's own membership row."""
    target = Project.objects.create(name=name, identifier=identifier, workspace=workspace)
    ProjectMember.objects.create(project=target, member=create_user, workspace=workspace, role=role, is_active=True)
    if archived:
        Project.objects.filter(pk=target.pk).update(archived_at=timezone.now())
        target.refresh_from_db()
    return target


def make_foreign_project(workspace, create_user, *, role=20):
    """A project in *another* workspace, with the caller's membership row."""
    other_workspace = Workspace.objects.create(name="Elsewhere WS", slug="elsewhere-ws", owner=create_user)
    WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=20, is_active=True)
    foreign = Project.objects.create(name="Foreign", identifier="FOR", workspace=other_workspace)
    ProjectMember.objects.create(
        project=foreign, member=create_user, workspace=other_workspace, role=role, is_active=True
    )
    return foreign


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
            entity_identifier=f"SRC-{with_link.sequence_id}",
        )

    return file_object


def add_revision(file_object, *, content=REVISION_BYTES, stored_objects):
    """Add a stored, superseded ``v2`` to ``file_object`` (a copy must carry it)."""
    key = f"{file_object.object_key}.v2"
    assert S3Storage().upload_file(io.BytesIO(content), key, content_type=file_object.mime_type) is True
    stored_objects.append(key)
    FileVersion.objects.create(
        project=file_object.project,
        file=file_object,
        version_no=2,
        object_key=key,
        bucket="uploads",
        mime_type=file_object.mime_type,
        size_bytes=len(content),
        status=FileVersion.Status.SUPERSEDED,
        is_active=False,
    )
    return key


def object_exists(key):
    return S3Storage().get_object_metadata(key) is not None


def stored_keys(prefix):
    """Every key the bucket holds under ``prefix`` (the store's own answer)."""
    storage = S3Storage()
    listed = storage.s3_client.list_objects_v2(Bucket=storage.aws_storage_bucket_name, Prefix=prefix)
    return sorted(item["Key"] for item in listed.get("Contents", []))


def project_prefix(project):
    return f"workspace/{project.workspace.slug}/projects/{project.ensure_storage_key()}/"


def charge(project, *, used_bytes):
    """Put the counters where an upload of ``project``'s files would have left them.

    The fixtures create rows and objects directly, so nothing has charged the source
    project yet; charging it here is what makes "the target pays, the source does not"
    and "the bytes move rather than double" assertions about real movement instead of
    about zero.
    """
    usage, _ = ProjectStorageUsage.all_objects.get_or_create(project=project)
    ProjectStorageUsage.objects.filter(pk=usage.pk).update(used_bytes=used_bytes, reserved_bytes=0)
    quota, _ = StorageQuota.all_objects.get_or_create(workspace=project.workspace)
    StorageQuota.objects.filter(pk=quota.pk).update(used_bytes=used_bytes, reserved_bytes=0)


def usage_of(project):
    row = ProjectStorageUsage.objects.filter(project=project).first()
    return None if row is None else (row.used_bytes, row.reserved_bytes)


def workspace_usage(workspace):
    row = StorageQuota.objects.filter(workspace=workspace).first()
    return None if row is None else (row.used_bytes, row.reserved_bytes)


def assert_target_untouched(target, *, objects_before):
    """The target holds nothing: no rows, no added bytes, no counters.

    ``objects_before`` is the target prefix's listing taken before the request, so the
    assertion is about what this test did rather than about whatever else the shared
    bucket happens to hold.
    """
    assert FileObject.objects.filter(project=target).count() == 0
    assert stored_keys(project_prefix(target)) == objects_before
    assert usage_of(target) in (None, (0, 0))


@pytest.mark.contract
@pytest.mark.django_db
class TestCrossProjectCopy:
    """AC-41: a member with write rights in both projects copies a file across them."""

    def test_copy_mints_a_target_prefixed_key_carries_versions_and_charges_the_target(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        source = make_file(project, name="Carried.pdf", stored_objects=stored_objects)
        add_revision(source, stored_objects=stored_objects)
        source_keys = set(FileVersion.objects.filter(file=source).values_list("object_key", flat=True))
        total_bytes = len(PDF_BYTES) + len(REVISION_BYTES)
        charge(project, used_bytes=total_bytes)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        copied_versions = list(FileVersion.objects.filter(file=copy).order_by("version_no"))
        stored_objects += [version.object_key for version in copied_versions]

        # A new identity, in the target, under the target project's own prefix.
        assert copy.id != source.id
        assert copy.project_id == target.id
        assert copy.workspace_id == project.workspace_id
        prefix = project_prefix(target)
        assert copy.object_key.startswith(prefix)
        assert all(version.object_key.startswith(prefix) for version in copied_versions)
        assert {version.object_key for version in copied_versions}.isdisjoint(source_keys)
        assert copy.category == source.category
        assert copy.visibility == source.visibility

        # Every version was copied, with the source's active pointer preserved.
        assert response.data["copied_versions"] == 2
        assert [version.version_no for version in copied_versions] == [1, 2]
        assert [version.is_active for version in copied_versions] == [True, False]
        assert copy.current_version_no == 1
        for version in copied_versions:
            assert object_exists(version.object_key)
            assert version.storage_metadata["copied_from"]["file_id"] == str(source.id)

        # The target pays; the source does not.
        assert usage_of(target) == (total_bytes, 0)
        assert response.data["target_storage_usage"]["project_used_bytes"] == total_bytes
        assert response.data["source_file_id"] == str(source.id)
        assert response.data["target_project_id"] == str(target.id)
        assert usage_of(project) == (total_bytes, 0)
        # A copy is a second copy of the bytes: the workspace holds both.
        assert workspace_usage(project.workspace) == (2 * total_bytes, 0)

        # ...and the source is exactly as it was: same row, same keys, same bytes.
        source.refresh_from_db()
        assert source.project_id == project.id
        assert set(FileVersion.objects.filter(file=source).values_list("object_key", flat=True)) == source_keys
        for key in source_keys:
            assert object_exists(key)

        # Both projects record the copy, each in its own feed, naming the other side.
        source_rows = FileAccessLog.objects.filter(file_id=source.id, action=FileAccessLog.Action.COPIED)
        target_rows = FileAccessLog.objects.filter(file_id=copy.id, action=FileAccessLog.Action.COPIED)
        assert source_rows.count() == 1
        assert target_rows.count() == 1
        assert source_rows.first().project_id == project.id
        assert target_rows.first().project_id == target.id
        assert source_rows.first().actor_id == create_user.id
        assert source_rows.first().metadata["operation"] == "copy_to_project"
        assert source_rows.first().metadata["target_project_id"] == str(target.id)
        assert source_rows.first().metadata["target_file_id"] == str(copy.id)
        assert target_rows.first().metadata["source_file_id"] == str(source.id)
        assert target_rows.first().metadata["source_project_id"] == str(project.id)
        assert target_rows.first().metadata["copied_versions"] == 2

    def test_copy_carries_no_entity_links(self, session_client, project, create_user, stored_objects):
        target = make_target(project.workspace, create_user)
        issue = Issue.objects.create(
            name="Linked",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        source = make_file(project, name="Linked.pdf", stored_objects=stored_objects, with_link=issue)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects += list(FileVersion.objects.filter(file=copy).values_list("object_key", flat=True))

        # Links are project-scoped: the copy starts unattached, the source keeps its own.
        assert response.data["file"]["link_count"] == 0
        assert FileLink.objects.filter(file=copy).count() == 0
        assert FileLink.objects.filter(file=source).count() == 1
        assert FileLink.objects.filter(entity_id=issue.id).count() == 1

    def test_copy_into_a_target_folder_and_suffixes_a_taken_name(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        target_folder = FileFolder.objects.create(
            project=target, name="Contracts", name_normalized=normalize_name("Contracts"), depth=0
        )
        make_file(target, name="Same.pdf", folder=target_folder, stored_objects=stored_objects)
        source = make_file(project, name="Same.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id), "folder_id": str(target_folder.id), "name_display": "Same.pdf"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects += list(FileVersion.objects.filter(file=copy).values_list("object_key", flat=True))

        assert copy.folder_id == target_folder.id
        assert copy.name_display == "Same (2).pdf"

    def test_copy_refuses_a_folder_of_the_source_project(self, session_client, project, create_user, stored_objects):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        foreign_folder = FileFolder.objects.create(
            project=project, name="Mine", name_normalized=normalize_name("Mine"), depth=0
        )
        source = make_file(project, name="Folder.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id), "folder_id": str(foreign_folder.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "folder_not_found"
        assert_target_untouched(target, objects_before=objects_before)

    def test_copy_requires_a_target_project_id(self, session_client, project, stored_objects):
        source = make_file(project, name="NoTarget.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id), {}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "invalid_request"
        assert response.data["field"] == "target_project_id"
        assert FileObject.objects.filter(project=project).count() == 1

    def test_copy_refuses_the_source_project_as_its_own_target(self, session_client, project, stored_objects):
        source = make_file(project, name="Itself.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(project.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "invalid_request"
        assert response.data["field"] == "target_project_id"
        assert FileObject.objects.filter(project=project).count() == 1

    def test_copy_refuses_a_target_the_caller_can_only_read(
        self, session_client, project, create_user, stored_objects
    ):
        """AC-41's other half: write rights are required in **both** projects."""
        target = make_target(project.workspace, create_user, name="Read Only", identifier="RO", role=5)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="BothSides.pdf", stored_objects=stored_objects)
        charge(project, used_bytes=len(PDF_BYTES))

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.data
        assert response.data["code"] == "permission_denied"
        assert response.data["target_project_id"] == str(target.id)
        assert "write access" in response.data["error"]
        assert_target_untouched(target, objects_before=objects_before)
        assert FileObject.objects.filter(project=project).count() == 1
        assert object_exists(source.object_key)
        assert usage_of(project) == (len(PDF_BYTES), 0)
        assert workspace_usage(project.workspace) == (len(PDF_BYTES), 0)

    def test_copy_refuses_a_target_the_caller_is_not_a_member_of(
        self, session_client, project, stored_objects
    ):
        stranger_project = Project.objects.create(name="Not Mine", identifier="NM", workspace=project.workspace)
        objects_before = stored_keys(project_prefix(stranger_project))
        source = make_file(project, name="Stranger.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(stranger_project.id)},
            format="json",
        )

        # The same neutral 404 a non-member gets everywhere else: this door confirms
        # nothing about a project the caller cannot see.
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.data
        assert response.data == {"error": "The required object does not exist."}
        assert_target_untouched(stranger_project, objects_before=objects_before)

    def test_copy_refuses_a_target_in_another_workspace(self, session_client, project, create_user, stored_objects):
        foreign = make_foreign_project(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(foreign))
        source = make_file(project, name="NoCrossWs.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(foreign.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "cross_workspace_not_supported"
        assert response.data["field"] == "target_project_id"
        assert_target_untouched(foreign, objects_before=objects_before)
        assert FileObject.objects.filter(project=project).count() == 1

    def test_copy_refuses_an_archived_target(self, session_client, project, create_user, stored_objects):
        target = make_target(project.workspace, create_user, name="Archived", identifier="ARC", archived=True)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="IntoArchived.pdf", stored_objects=stored_objects)

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.data
        assert response.data["code"] == "project_archived"
        assert_target_untouched(target, objects_before=objects_before)

    def test_copy_is_refused_when_the_target_project_cannot_hold_it(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        usage, _ = ProjectStorageUsage.all_objects.get_or_create(project=target)
        ProjectStorageUsage.objects.filter(pk=usage.pk).update(limit_bytes=1)
        source = make_file(project, name="TooBigForTarget.pdf", stored_objects=stored_objects)
        charge(project, used_bytes=len(PDF_BYTES))

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "quota_exceeded"
        assert response.data["level"] == "project"
        assert response.data["limit_bytes"] == 1
        # Refused before a single byte was duplicated, and nothing was charged.
        assert_target_untouched(target, objects_before=objects_before)
        assert workspace_usage(project.workspace) == (len(PDF_BYTES), 0)
        assert object_exists(source.object_key)

    def test_copy_refuses_a_trashed_source(self, session_client, project, create_user, stored_objects):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="Trashed.pdf", stored_objects=stored_objects)
        session_client.delete(f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/{source.id}/")

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.data
        assert response.data["code"] == "file_trashed"
        assert_target_untouched(target, objects_before=objects_before)

    def test_copy_refuses_a_source_without_an_active_version(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(
            project,
            name="Superseded.pdf",
            stored_objects=stored_objects,
            version_status=FileVersion.Status.SUPERSEDED,
            is_active=False,
        )

        response = session_client.post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.data
        assert response.data["code"] == "object_unavailable"
        assert_target_untouched(target, objects_before=objects_before)

    def test_a_guest_in_the_source_project_is_refused(self, project, create_user, stored_objects):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="GuestSource.pdf", stored_objects=stored_objects)
        guest = add_member(project, email="guest-source@example.com", role=5)

        response = client_for(guest).post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.data
        assert response.data["code"] == "permission_denied"
        assert_target_untouched(target, objects_before=objects_before)

    def test_a_non_member_of_the_source_project_gets_the_generic_404(
        self, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="Private.pdf", stored_objects=stored_objects)
        outsider = add_member(project, email="outsider@example.com", role=20, active=False)

        response = client_for(outsider).post(
            copy_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND, response.data
        assert response.data == {"error": "The required object does not exist."}
        assert_target_untouched(target, objects_before=objects_before)


@pytest.mark.contract
@pytest.mark.django_db
class TestCrossProjectMove:
    """AC-42: a move copies, verifies the copy, purges the source, leaves one copy."""

    def test_move_verifies_then_purges_the_source_and_leaves_exactly_one_copy(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        source = make_file(project, name="Moving.pdf", stored_objects=stored_objects)
        revision_key = add_revision(source, stored_objects=stored_objects)
        source_keys = {source.object_key, revision_key}
        total_bytes = len(PDF_BYTES) + len(REVISION_BYTES)
        charge(project, used_bytes=total_bytes)

        response = session_client.post(
            move_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["source_purged"] is True
        assert response.data["source_file_id"] == str(source.id)
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        copied_versions = list(FileVersion.objects.filter(file=copy).order_by("version_no"))
        stored_objects += [version.object_key for version in copied_versions]

        # Exactly one copy: one row in the target, none in the source, and every source
        # object gone from the store.
        assert FileObject.objects.filter(project=project).count() == 0
        assert FileObject.all_objects.filter(project=project).count() == 0
        assert FileObject.objects.filter(project=target).count() == 1
        assert FileObject.objects.filter(project=target, name_normalized=normalize_name("Moving.pdf")).count() == 1
        assert copy.current_version_no == 1
        assert [version.is_active for version in copied_versions] == [True, False]
        for key in source_keys:
            assert not object_exists(key)
        for version in copied_versions:
            assert object_exists(version.object_key)

        # The bytes moved rather than doubled: one copy of them, in the target, charged
        # once in the workspace - never to both projects at once.
        assert usage_of(target) == (total_bytes, 0)
        assert usage_of(project) == (0, 0)
        assert workspace_usage(project.workspace) == (total_bytes, 0)

        # Both feeds record the move; the source's own trail also shows the mechanism.
        source_moves = FileAccessLog.objects.filter(file_id=source.id, action=FileAccessLog.Action.MOVED)
        target_moves = FileAccessLog.objects.filter(file_id=copy.id, action=FileAccessLog.Action.MOVED)
        assert source_moves.count() == 1
        assert target_moves.count() == 1
        assert source_moves.first().project_id == project.id
        assert target_moves.first().project_id == target.id
        assert source_moves.first().metadata["operation"] == "move_to_project"
        assert source_moves.first().metadata["target_file_id"] == str(copy.id)
        assert target_moves.first().metadata["source_file_id"] == str(source.id)
        purged = FileAccessLog.objects.filter(file_id=source.id, action=FileAccessLog.Action.PURGED)
        assert purged.count() == 1
        assert purged.first().metadata["trigger"] == "move_to_project"

    @pytest.mark.parametrize(
        "mutation, reason",
        [
            ({"ETag": '"00000000000000000000000000000000"'}, "etag_mismatch"),
            ({"ContentLength": 1}, "size_mismatch"),
            ({"ContentType": "text/plain"}, "mime_mismatch"),
            (None, "object_missing"),
        ],
    )
    def test_a_move_whose_copy_fails_verification_keeps_the_source(
        self, session_client, project, create_user, stored_objects, mutation, reason
    ):
        """The one that matters: a corrupted copy must never cost the source its file."""
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="VerifyMe.pdf", stored_objects=stored_objects)
        source_keys = set(FileVersion.objects.filter(file=source).values_list("object_key", flat=True))
        charge(project, used_bytes=len(PDF_BYTES))
        prefix = project_prefix(target)

        real_get_object_metadata = S3Storage.get_object_metadata

        def corrupted(self, object_name):
            """Corrupt exactly what the target project's prefix answers with."""
            metadata = real_get_object_metadata(self, object_name)
            if metadata is None or not object_name.startswith(prefix):
                return metadata
            if mutation is None:
                return None
            return {**metadata, **mutation}

        with mock.patch.object(S3Storage, "get_object_metadata", corrupted):
            response = session_client.post(
                move_to_project_url(project.workspace.slug, project.id, source.id),
                {"target_project_id": str(target.id)},
                format="json",
            )

        assert response.status_code == status.HTTP_409_CONFLICT, response.data
        assert response.data["code"] == "verification_failed"
        assert response.data["reason"] == reason
        assert response.data["version_no"] == 1

        # The source survived, byte for byte, with its row, its keys and its counters.
        source.refresh_from_db()
        assert source.status == FileObject.Status.ACTIVE
        assert source.deleted_at is None
        assert source.project_id == project.id
        assert set(FileVersion.objects.filter(file=source).values_list("object_key", flat=True)) == source_keys
        for key in source_keys:
            assert object_exists(key)
        assert usage_of(project) == (len(PDF_BYTES), 0)
        assert workspace_usage(project.workspace) == (len(PDF_BYTES), 0)

        # ...and the failed move left nothing at all in the target: no rows, no bytes
        # (the copied objects were removed), no audit row, no charged quota. A move that
        # cannot verify is not a copy.
        assert_target_untouched(target, objects_before=objects_before)
        assert FileAccessLog.objects.filter(project=target).count() == 0

    def test_a_move_whose_source_purge_fails_reports_it_and_leaves_the_source_in_the_trash(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        source = make_file(project, name="PurgeFails.pdf", stored_objects=stored_objects)
        charge(project, used_bytes=len(PDF_BYTES))

        with mock.patch.object(S3Storage, "delete_files", return_value=False):
            response = session_client.post(
                move_to_project_url(project.workspace.slug, project.id, source.id),
                {"target_project_id": str(target.id)},
                format="json",
            )

        assert response.status_code == status.HTTP_502_BAD_GATEWAY, response.data
        assert response.data["code"] == "storage_unavailable"
        assert response.data["source_file_id"] == str(source.id)
        # The verified copy is in the target, and the source is recoverable in the trash:
        # nothing was silently deleted, and the failure is reported rather than retried
        # as a delete (R-OPS-4).
        copy = FileObject.objects.get(project=target, name_normalized=normalize_name("PurgeFails.pdf"))
        stored_objects += list(FileVersion.objects.filter(file=copy).values_list("object_key", flat=True))
        source.refresh_from_db()
        assert source.status == FileObject.Status.PURGE_FAILED
        assert source.deleted_at is not None
        assert object_exists(source.object_key)
        assert FileAccessLog.objects.filter(file_id=source.id, action=FileAccessLog.Action.TRASHED).count() == 1

    def test_move_refuses_a_target_the_caller_can_only_read(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user, name="Read Only", identifier="RO", role=5)
        objects_before = stored_keys(project_prefix(target))
        source = make_file(project, name="StaysHere.pdf", stored_objects=stored_objects)

        response = session_client.post(
            move_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.data
        assert response.data["code"] == "permission_denied"
        assert_target_untouched(target, objects_before=objects_before)
        source.refresh_from_db()
        assert source.status == FileObject.Status.ACTIVE
        assert object_exists(source.object_key)

    def test_move_refuses_a_target_in_another_workspace(self, session_client, project, create_user, stored_objects):
        foreign = make_foreign_project(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(foreign))
        source = make_file(project, name="SameWsOnly.pdf", stored_objects=stored_objects)

        response = session_client.post(
            move_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(foreign.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "cross_workspace_not_supported"
        assert_target_untouched(foreign, objects_before=objects_before)
        source.refresh_from_db()
        assert source.status == FileObject.Status.ACTIVE
        assert object_exists(source.object_key)

    def test_move_is_refused_when_the_target_cannot_hold_the_file(
        self, session_client, project, create_user, stored_objects
    ):
        target = make_target(project.workspace, create_user)
        objects_before = stored_keys(project_prefix(target))
        usage, _ = ProjectStorageUsage.all_objects.get_or_create(project=target)
        ProjectStorageUsage.objects.filter(pk=usage.pk).update(limit_bytes=1)
        source = make_file(project, name="NoRoom.pdf", stored_objects=stored_objects)

        response = session_client.post(
            move_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["code"] == "quota_exceeded"
        assert response.data["level"] == "project"
        assert_target_untouched(target, objects_before=objects_before)
        source.refresh_from_db()
        assert source.status == FileObject.Status.ACTIVE
        assert source.deleted_at is None
        assert object_exists(source.object_key)

    def test_a_move_drops_the_source_file_links(self, session_client, project, create_user, stored_objects):
        """R-OPS-4: for a move the source's links stop being live and the copy has none."""
        target = make_target(project.workspace, create_user)
        issue = Issue.objects.create(
            name="Linked move",
            project=project,
            workspace=project.workspace,
            state=State.objects.create(
                name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
            ),
        )
        source = make_file(project, name="MoveLinked.pdf", stored_objects=stored_objects, with_link=issue)

        response = session_client.post(
            move_to_project_url(project.workspace.slug, project.id, source.id),
            {"target_project_id": str(target.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        copy = FileObject.objects.get(id=response.data["file"]["id"])
        stored_objects += list(FileVersion.objects.filter(file=copy).values_list("object_key", flat=True))

        # The entity no longer surfaces the file (the link went with the purged row),
        # and the copy carries none of the source's links.
        assert FileLink.objects.filter(entity_id=issue.id).count() == 0
        assert FileLink.objects.filter(file=copy).count() == 0
