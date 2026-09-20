# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for project file listing, detail, search and filters.

Acceptance: AC-01, AC-02, AC-14, AC-37. These tests are database-only; the
listing path must never touch the object store (R-NFR-1), which is asserted with
a patched storage class rather than by accident.
"""

# Python imports
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from unittest import mock

# Django imports
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

# Third party imports
import pytest
from rest_framework import status
from rest_framework.test import APIClient

# Module imports
from plane.db.models import (
    FileFolder,
    FileLink,
    FileObject,
    FileVersion,
    Issue,
    Page,
    Project,
    ProjectMember,
    ProjectPage,
    ProjectStorageUsage,
    State,
    StorageQuota,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.file_storage.naming import normalize_name

PDF = "application/pdf"
MARKDOWN = "text/markdown"
CSV = "text/csv"
PNG = "image/png"


def list_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/"


def detail_url(slug, project_id, file_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/files/{file_id}/"


@pytest.fixture
def workspace(create_user):
    workspace = Workspace.objects.create(name="Listing Workspace", slug="listing-workspace", owner=create_user)
    WorkspaceMember.objects.create(workspace=workspace, member=create_user, role=20, is_active=True)
    return workspace


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(name="Listing Project", identifier="LIST", workspace=workspace)
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20, is_active=True)
    return project


@pytest.fixture
def other_project(workspace, create_user):
    project = Project.objects.create(name="Other Project", identifier="OTHR", workspace=workspace)
    return project


def add_member(project, *, email, role, workspace_role=None):
    user = User.objects.create(email=email, username=email.split("@")[0], first_name=email.split("@")[0])
    user.set_password("test-password")
    user.save()

    if workspace_role is not None:
        WorkspaceMember.objects.create(
            workspace=project.workspace, member=user, role=workspace_role, is_active=True
        )
    ProjectMember.objects.create(project=project, member=user, workspace=project.workspace, role=role, is_active=True)
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_file(
    project,
    *,
    name,
    mime=PDF,
    extension="pdf",
    size=100,
    folder=None,
    pinned=False,
    trashed=False,
    uploader=None,
    created_at=None,
    category=FileObject.Category.DOCS,
):
    file_object = FileObject(
        project=project,
        folder=folder,
        name_original=name,
        name_display=name,
        name_normalized=normalize_name(name),
        mime_type=mime,
        extension=extension,
        bucket="uploads",
        size_bytes=size,
        object_key=f"{uuid.uuid4()}.{extension}",
        category=category,
        status=FileObject.Status.TRASHED if trashed else FileObject.Status.ACTIVE,
        is_pinned=pinned,
        current_version_no=1,
    )
    file_object.save(force_insert=True, created_by_id=uploader.id if uploader else None)
    if created_at is not None:
        FileObject.objects.filter(pk=file_object.pk).update(created_at=created_at)
        file_object.created_at = created_at
    return file_object


def make_version(file_object, version_no=1, *, is_active=True, size=100, uploaded_by=None):
    return FileVersion.objects.create(
        project=file_object.project,
        file=file_object,
        uploaded_by=uploaded_by,
        version_no=version_no,
        object_key=f"{file_object.object_key}.v{version_no}",
        bucket="uploads",
        mime_type=file_object.mime_type,
        size_bytes=size,
        status=FileVersion.Status.ACTIVE if is_active else FileVersion.Status.SUPERSEDED,
        is_active=is_active,
    )


def names(response):
    return [row["name_display"] for row in response.data["results"]]


@pytest.fixture
def library(project, create_user):
    """A small tree: two folders (one nested), seven files with distinct facets."""
    specs = FileFolder.objects.create(project=project, name="Specs", name_normalized="specs")
    sub = FileFolder.objects.create(project=project, parent=specs, name="Sub", name_normalized="sub", depth=1)
    plans = FileFolder.objects.create(project=project, name="Plans", name_normalized="plans")

    contributor = add_member(project, email="contributor@example.com", role=15)

    state = State.objects.create(
        name="Todo", color="#60646C", group="unstarted", project=project, workspace=project.workspace
    )
    issue = Issue.objects.create(name="Linked work", project=project, workspace=project.workspace, state=state)

    def stamp(day):
        return datetime(2026, day, 5, 12, 0, tzinfo=dt_timezone.utc)

    alpha = make_file(
        project,
        name="Alpha Report.pdf",
        size=100,
        pinned=True,
        uploader=create_user,
        created_at=stamp(1),
    )
    beta = make_file(
        project,
        name="Beta Notes.md",
        mime=MARKDOWN,
        extension="md",
        size=200,
        uploader=create_user,
        created_at=stamp(2),
    )
    gamma = make_file(
        project,
        name="Gamma Sheet.csv",
        mime=CSV,
        extension="csv",
        size=300,
        folder=specs,
        uploader=contributor,
        created_at=stamp(3),
    )
    delta = make_file(
        project,
        name="Delta Trash.pdf",
        size=400,
        trashed=True,
        uploader=create_user,
        created_at=stamp(4),
    )
    epsilon = make_file(
        project,
        name="Epsilon Image.png",
        mime=PNG,
        extension="png",
        size=500,
        folder=specs,
        pinned=True,
        uploader=contributor,
        created_at=stamp(5),
    )
    zeta = make_file(
        project,
        name="Zeta Report.pdf",
        size=600,
        folder=sub,
        uploader=contributor,
        created_at=stamp(6),
    )
    unicode_file = make_file(project, name="รายงานประจำเดือน.pdf", size=700, uploader=contributor, created_at=stamp(7))

    for file_object in (alpha, gamma):
        FileLink.objects.create(
            project=project,
            file=file_object,
            entity_type=FileLink.EntityType.ISSUE,
            entity_id=issue.id,
            entity_identifier=f"LIST-{issue.sequence_id}",
        )

    return {
        "root": None,
        "specs": specs,
        "sub": sub,
        "plans": plans,
        "issue": issue,
        "contributor": contributor,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "delta": delta,
        "epsilon": epsilon,
        "zeta": zeta,
        "unicode": unicode_file,
    }


@pytest.mark.contract
@pytest.mark.django_db
class TestFileListing:
    """AC-01 and AC-14: project-scoped listing, filtering and paging."""

    def test_lists_only_the_projects_own_live_files(self, session_client, project, library, other_project):
        make_file(other_project, name="Foreign.pdf")

        response = session_client.get(list_url(project.workspace.slug, project.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert "Foreign.pdf" not in names(response)
        assert "Delta Trash.pdf" not in names(response)  # trash is opt-in
        assert set(names(response)) == {
            "Alpha Report.pdf",
            "Beta Notes.md",
            "Gamma Sheet.csv",
            "Epsilon Image.png",
            "Zeta Report.pdf",
            "รายงานประจำเดือน.pdf",
        }
        assert response.data["page"]["total_results"] == 6

    def test_pagination_walks_two_pages_without_losses(self, session_client, project, library):
        first = session_client.get(list_url(project.workspace.slug, project.id), {"page_size": 4})

        assert first.status_code == status.HTTP_200_OK
        assert len(first.data["results"]) == 4
        assert first.data["page"]["next_cursor"]
        assert first.data["page"]["next_page_results"] is True
        assert first.data["page"]["total_results"] == 6

        second = session_client.get(
            list_url(project.workspace.slug, project.id), {"cursor": first.data["page"]["next_cursor"]}
        )

        assert second.status_code == status.HTTP_200_OK
        assert len(second.data["results"]) == 2
        assert second.data["page"]["next_cursor"] is None
        assert second.data["page"]["prev_page_results"] is True

        walked = names(first) + names(second)
        assert len(walked) == 6
        assert len(set(walked)) == 6

    def test_a_malformed_cursor_is_rejected(self, session_client, project, library):
        response = session_client.get(list_url(project.workspace.slug, project.id), {"cursor": "not-a-cursor"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data["code"] == "invalid_request"
        assert response.data["field"] == "cursor"

    @pytest.mark.parametrize(
        "params,expected",
        [
            # q + mime + extension
            ({"q": "report", "mime": PDF, "extension": "pdf"}, {"Alpha Report.pdf", "Zeta Report.pdf"}),
            # folder + q + size
            (
                {"folder_id": "SPECS", "q": "gamma", "size_min": 200},
                {"Gamma Sheet.csv"},
            ),
            # uploader + created range + extension
            (
                {
                    "uploader": "CONTRIBUTOR",
                    "created_from": "2026-03-01",
                    "created_to": "2026-05-31",
                    "extension": "csv",
                },
                {"Gamma Sheet.csv"},
            ),
            # size range + folder + pinned
            (
                {"size_min": 50, "size_max": 150, "folder_id": "root", "pinned": "true"},
                {"Alpha Report.pdf"},
            ),
            # entity + mime (three filters: entity_type, entity_id, mime)
            (
                {"entity_type": "issue", "entity_id": "ISSUE", "mime": PDF},
                {"Alpha Report.pdf"},
            ),
            # trash + mime + size
            ({"trashed": "true", "mime": PDF, "size_min": 1}, {"Delta Trash.pdf"}),
            # folder + pinned
            ({"folder_id": "SPECS", "pinned": "true"}, {"Epsilon Image.png"}),
            # ordering + trashed=false + mime
            ({"ordering": "name", "trashed": "false", "mime": PNG}, {"Epsilon Image.png"}),
        ],
    )
    def test_filters_combine_conjunctively(self, session_client, project, library, params, expected):
        identifiers = {
            "ISSUE": str(library["issue"].id),
            "SPECS": str(library["specs"].id),
            "CONTRIBUTOR": str(library["contributor"].id),
        }
        resolved = {key: identifiers.get(value, value) for key, value in params.items()}

        response = session_client.get(list_url(project.workspace.slug, project.id), resolved)

        assert response.status_code == status.HTTP_200_OK, response.data
        assert set(names(response)) == expected

    def test_folder_scope_and_breadcrumbs(self, session_client, project, library):
        nested = session_client.get(
            list_url(project.workspace.slug, project.id), {"folder_id": str(library["sub"].id)}
        )

        assert nested.status_code == status.HTTP_200_OK
        assert names(nested) == ["Zeta Report.pdf"]
        assert [crumb["name"] for crumb in nested.data["breadcrumbs"]] == ["Specs", "Sub"]
        assert nested.data["folders"] == []

        parent = session_client.get(
            list_url(project.workspace.slug, project.id), {"folder_id": str(library["specs"].id)}
        )

        assert sorted(names(parent)) == ["Epsilon Image.png", "Gamma Sheet.csv"]
        assert [crumb["name"] for crumb in parent.data["breadcrumbs"]] == ["Specs"]
        assert [folder["name"] for folder in parent.data["folders"]] == ["Sub"]

        root = session_client.get(list_url(project.workspace.slug, project.id), {"folder_id": "root"})

        assert root.data["breadcrumbs"] == []
        assert [folder["name"] for folder in root.data["folders"]] == ["Plans", "Specs"]
        assert set(names(root)) == {
            "Alpha Report.pdf",
            "Beta Notes.md",
            "รายงานประจำเดือน.pdf",
        }

    def test_unknown_folder_is_not_found(self, session_client, project, library):
        response = session_client.get(list_url(project.workspace.slug, project.id), {"folder_id": str(uuid.uuid4())})

        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.parametrize(
        "hostile",
        [
            "' OR 1=1 --",
            '"; DROP TABLE projects; --',
            "%00",
            "../../etc/passwd",
            "报告' OR '1'='1",
            "a" * 300,
        ],
    )
    def test_hostile_name_search_is_safe_and_empty(self, session_client, project, library, hostile):
        response = session_client.get(list_url(project.workspace.slug, project.id), {"q": hostile})

        assert response.status_code == status.HTTP_200_OK, response.data
        assert names(response) == []

        # Nothing was injected: the project and its files are still there.
        assert Project.objects.filter(pk=project.pk).exists()
        assert FileObject.objects.filter(project=project).count() == 7

    def test_unicode_search_matches_the_stored_name(self, session_client, project, library):
        response = session_client.get(list_url(project.workspace.slug, project.id), {"q": "รายงาน"})

        assert response.status_code == status.HTTP_200_OK
        assert names(response) == ["รายงานประจำเดือน.pdf"]

    def test_ordering_is_stable_across_pages(self, session_client, project):
        # Ten files with identical sizes and timestamps: only the id tiebreaker
        # can keep pagination deterministic.
        stamp = datetime(2026, 7, 5, 12, 0, tzinfo=dt_timezone.utc)
        for index in range(10):
            make_file(project, name=f"Tie {index}.pdf", size=100, created_at=stamp)

        single = session_client.get(list_url(project.workspace.slug, project.id), {"ordering": "size"})
        walked = []
        cursor = None
        for _ in range(10):
            params = {"ordering": "size", "page_size": 3}
            if cursor:
                params["cursor"] = cursor
            page = session_client.get(list_url(project.workspace.slug, project.id), params)
            assert page.status_code == status.HTTP_200_OK
            walked.extend(row["id"] for row in page.data["results"])
            cursor = page.data["page"]["next_cursor"]
            if not cursor:
                break

        assert walked == [row["id"] for row in single.data["results"]]
        assert len(set(walked)) == 10

    def test_ordering_by_name_and_size(self, session_client, project, library):
        by_name = session_client.get(list_url(project.workspace.slug, project.id), {"ordering": "name"})

        # The collation decides where the Thai name lands, so only the ASCII
        # order is pinned.
        assert names(by_name)[:5] == [
            "Alpha Report.pdf",
            "Beta Notes.md",
            "Epsilon Image.png",
            "Gamma Sheet.csv",
            "Zeta Report.pdf",
        ]
        assert "รายงานประจำเดือน.pdf" in names(by_name)

        by_size = session_client.get(list_url(project.workspace.slug, project.id), {"ordering": "-size"})

        assert names(by_size)[0] == "รายงานประจำเดือน.pdf"

    def test_bad_filter_values_are_rejected(self, session_client, project, library):
        for params, field in [
            ({"ordering": "drop"}, "ordering"),
            ({"size_min": "abc"}, "size_min"),
            ({"created_from": "not-a-date"}, "created_from"),
            ({"pinned": "maybe"}, "pinned"),
            ({"entity_type": "unknown"}, "entity_type"),
            ({"folder_id": "not-a-uuid"}, "folder_id"),
        ]:
            response = session_client.get(list_url(project.workspace.slug, project.id), params)

            assert response.status_code == status.HTTP_400_BAD_REQUEST, params
            assert response.data["code"] == "invalid_request"
            assert response.data["field"] == field

    def test_listing_never_calls_the_object_store(self, session_client, project, library):
        with mock.patch("plane.settings.storage.S3Storage") as storage:
            response = session_client.get(list_url(project.workspace.slug, project.id))

        assert response.status_code == status.HTTP_200_OK
        storage.assert_not_called()

    def test_storage_summary_counts_trashed_files_on_purpose(self, session_client, project, library):
        """AD-09: trashed files keep consuming quota until they are purged."""
        response = session_client.get(list_url(project.workspace.slug, project.id))

        assert FileObject.objects.filter(project=project, status=FileObject.Status.TRASHED).count() == 1
        assert "Delta Trash.pdf" not in names(response)
        assert response.data["storage"]["file_count"] == 7

    def test_link_count_is_stable_with_and_without_entity_filters(
        self, session_client, project, library, create_user
    ):
        """An entity filter must not shrink the link count of the matching file."""
        alpha = library["alpha"]
        page = Page.objects.create(
            name="Linked page",
            workspace=project.workspace,
            owned_by=create_user,
        )
        ProjectPage.objects.create(project=project, page=page, workspace=project.workspace)
        FileLink.objects.create(
            project=project,
            file=alpha,
            entity_type=FileLink.EntityType.PAGE,
            entity_id=page.id,
            entity_identifier=str(page.id),
        )

        unfiltered = session_client.get(list_url(project.workspace.slug, project.id), {"q": "Alpha"})
        issue_filtered = session_client.get(
            list_url(project.workspace.slug, project.id), {"q": "Alpha", "entity_type": "issue"}
        )
        entity_filtered = session_client.get(
            list_url(project.workspace.slug, project.id),
            {"q": "Alpha", "entity_type": "issue", "entity_id": str(library["issue"].id)},
        )
        detail = session_client.get(detail_url(project.workspace.slug, project.id, alpha.id))

        assert unfiltered.data["results"][0]["link_count"] == 2
        assert issue_filtered.data["results"][0]["link_count"] == 2
        assert entity_filtered.data["results"][0]["link_count"] == 2
        assert detail.data["link_count"] == 2
        assert detail.data["file"]["link_count"] == 2

    def test_listing_survives_a_soft_deleted_quota_row(self, session_client, project, library):
        """A soft-deleted counter row is revived, never duplicated."""
        StorageQuota.objects.filter(workspace=project.workspace).update(deleted_at=timezone.now())
        ProjectStorageUsage.objects.filter(project=project).update(deleted_at=timezone.now())

        response = session_client.get(list_url(project.workspace.slug, project.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["storage"]["file_count"] == 7
        assert StorageQuota.all_objects.filter(workspace=project.workspace, deleted_at__isnull=True).count() == 1
        assert (
            ProjectStorageUsage.all_objects.filter(project=project, deleted_at__isnull=True).count() == 1
        )

    def test_storage_summary_reports_counts_and_ceilings(self, session_client, project, library):
        response = session_client.get(list_url(project.workspace.slug, project.id))

        storage = response.data["storage"]
        assert storage["file_count"] == 7
        assert storage["version_count"] == 0
        assert storage["project_used_bytes"] == 0
        assert storage["workspace_used_bytes"] == 0
        assert storage["limit_bytes"] is not None
        assert storage["warn_threshold_pct"] == 80

    def test_a_thousand_rows_stay_bounded_and_query_constant(self, session_client, project):
        """Cheap sanity check, not the recorded benchmark (that is T-120)."""
        stamp = datetime(2026, 8, 5, 12, 0, tzinfo=dt_timezone.utc)
        FileObject.objects.bulk_create(
            [
                FileObject(
                    id=uuid.uuid4(),
                    project=project,
                    workspace=project.workspace,
                    name_original=f"Row {index}.pdf",
                    name_display=f"Row {index}.pdf",
                    name_normalized=f"row {index}.pdf",
                    mime_type=PDF,
                    extension="pdf",
                    bucket="uploads",
                    size_bytes=index,
                    object_key=f"{uuid.uuid4()}.pdf",
                    category=FileObject.Category.DOCS,
                    status=FileObject.Status.ACTIVE,
                    created_at=stamp,
                    updated_at=stamp,
                )
                for index in range(1000)
            ]
        )

        # Warm up: the first request materialises the project's quota rows.
        session_client.get(list_url(project.workspace.slug, project.id), {"page_size": 5})

        with CaptureQueriesContext(connection) as small_page:
            response = session_client.get(list_url(project.workspace.slug, project.id), {"page_size": 5})
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["results"]) == 5
        assert response.data["page"]["total_results"] == 1000

        started = time.perf_counter()
        with CaptureQueriesContext(connection) as large_page:
            response = session_client.get(list_url(project.workspace.slug, project.id), {"page_size": 50})
        elapsed = time.perf_counter() - started

        assert len(response.data["results"]) == 50
        # The number of queries must not grow with the page size: no N+1.
        assert len(large_page.captured_queries) == len(small_page.captured_queries)
        assert elapsed < 2.0


@pytest.mark.contract
@pytest.mark.django_db
class TestFileDetail:
    """Detail returns versions, links and permissions for the caller."""

    def test_detail_exposes_the_creator_and_the_version_uploader(
        self, session_client, project, library, create_user
    ):
        """The file's creator and a version's uploader are distinct people."""
        alpha = library["alpha"]
        contributor = library["contributor"]
        make_version(alpha, version_no=1, is_active=True, uploaded_by=contributor)

        response = session_client.get(detail_url(project.workspace.slug, project.id, alpha.id))

        assert response.data["file"]["uploader"]["email"] == create_user.email
        assert response.data["version"]["uploaded_by"]["email"] == contributor.email

    def test_detail_returns_the_active_version_links_and_permissions(self, session_client, project, library):
        alpha = library["alpha"]
        make_version(alpha, version_no=1, is_active=True)
        make_version(alpha, version_no=2, is_active=False, size=120)

        response = session_client.get(detail_url(project.workspace.slug, project.id, alpha.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["file"]["id"] == str(alpha.id)
        assert response.data["file"]["link_count"] == 1
        assert response.data["link_count"] == 1
        assert [version["version_no"] for version in response.data["versions"]] == [2, 1]
        assert response.data["version"]["version_no"] == 1
        assert response.data["version"]["is_active"] is True
        assert [link["entity_type"] for link in response.data["links"]] == ["issue"]
        assert response.data["permissions"] == {"can_edit": True, "can_delete": True, "can_download": True}

    def test_detail_can_select_a_non_active_version(self, session_client, project, library):
        alpha = library["alpha"]
        make_version(alpha, version_no=1, is_active=True)
        make_version(alpha, version_no=2, is_active=False, size=120)

        response = session_client.get(
            detail_url(project.workspace.slug, project.id, alpha.id), {"version": "2"}
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["version"]["version_no"] == 2
        assert response.data["version"]["is_active"] is False
        # The active pointer is untouched.
        assert FileVersion.objects.get(file=alpha, version_no=1).is_active is True

    def test_unknown_version_is_not_found(self, session_client, project, library):
        alpha = library["alpha"]
        make_version(alpha, version_no=1)

        response = session_client.get(
            detail_url(project.workspace.slug, project.id, alpha.id), {"version": "9"}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_permissions_for_each_role(self, session_client, project, library):
        guest = add_member(project, email="guest@example.com", role=5)
        member = add_member(project, email="member@example.com", role=15)
        admin = add_member(project, email="admin@example.com", role=20)

        for user, expected in [
            (guest, {"can_edit": False, "can_delete": False, "can_download": True}),
            (member, {"can_edit": True, "can_delete": True, "can_download": True}),
            (admin, {"can_edit": True, "can_delete": True, "can_download": True}),
        ]:
            response = client_for(user).get(detail_url(project.workspace.slug, project.id, library["alpha"].id))

            assert response.status_code == status.HTTP_200_OK, (user.email, response.data)
            assert response.data["permissions"] == expected

    def test_a_guest_may_list_and_read_but_not_mutate(self, project, library):
        guest = add_member(project, email="guest-reader@example.com", role=5, workspace_role=5)
        client = client_for(guest)

        listing = client.get(list_url(project.workspace.slug, project.id))
        detail = client.get(detail_url(project.workspace.slug, project.id, library["alpha"].id))

        assert listing.status_code == status.HTTP_200_OK
        assert detail.status_code == status.HTTP_200_OK

        initiate = client.post(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/initiate-upload/",
            {"file_name": "Nope.pdf", "size_bytes": 10, "mime_type": PDF},
            format="json",
        )
        assert initiate.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
class TestIsolationAndLifecycle:
    """AC-02 and AC-37: cross-project reads, archived and deleted projects."""

    def test_a_file_from_another_project_is_not_found(self, session_client, project, other_project, create_user):
        ProjectMember.objects.create(
            project=other_project, member=create_user, workspace=other_project.workspace, role=20, is_active=True
        )
        foreign = make_file(other_project, name="Foreign.pdf")

        detail = session_client.get(detail_url(project.workspace.slug, project.id, foreign.id))
        listing = session_client.get(list_url(project.workspace.slug, project.id))

        assert detail.status_code == status.HTTP_404_NOT_FOUND
        # The same generic body the upload endpoints return.
        assert detail.data == {"error": "The required object does not exist."}
        assert "Foreign.pdf" not in names(listing)

    def test_a_non_member_cannot_list_or_read(self, project, library):
        outsider = User.objects.create(email="outsider@example.com", username="outsider")
        outsider.set_password("test-password")
        outsider.save()
        ProjectMember.objects.create(
            project=project, member=outsider, workspace=project.workspace, role=20, is_active=False
        )
        client = client_for(outsider)

        listing = client.get(list_url(project.workspace.slug, project.id))
        detail = client.get(detail_url(project.workspace.slug, project.id, library["alpha"].id))

        assert listing.status_code == status.HTTP_403_FORBIDDEN
        assert detail.status_code == status.HTTP_403_FORBIDDEN

    def test_an_archived_project_is_read_only(self, session_client, project, library, create_user):
        Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())

        listing = session_client.get(list_url(project.workspace.slug, project.id))
        detail = session_client.get(detail_url(project.workspace.slug, project.id, library["alpha"].id))
        initiate = session_client.post(
            f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/files/initiate-upload/",
            {"file_name": "Nope.pdf", "size_bytes": 10, "mime_type": PDF},
            format="json",
        )

        assert listing.status_code == status.HTTP_200_OK
        assert "Alpha Report.pdf" in names(listing)
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["permissions"] == {
            "can_edit": False,
            "can_delete": False,
            "can_download": True,
        }
        # The mutating endpoints refuse consistently.
        assert initiate.status_code == status.HTTP_409_CONFLICT
        assert initiate.data["code"] == "project_archived"

    def test_a_deleted_project_is_not_found(self, session_client, project, library):
        Project.objects.filter(pk=project.pk).update(deleted_at=timezone.now())

        listing = session_client.get(list_url(project.workspace.slug, project.id))
        detail = session_client.get(detail_url(project.workspace.slug, project.id, library["alpha"].id))

        assert listing.status_code == status.HTTP_404_NOT_FOUND
        assert detail.status_code == status.HTTP_404_NOT_FOUND
