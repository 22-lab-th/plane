# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for project page folders."""

import json
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Page, Project, ProjectMember, ProjectPage, WorkspaceMember, User


@pytest.fixture(autouse=True)
def mock_page_background_tasks(mocker):
    """Keep contract tests deterministic and independent of the Celery broker."""
    mocker.patch("plane.app.views.page.base.page_transaction.delay")
    mocker.patch("plane.app.views.page.base.recent_visited_task.delay")
    mocker.patch("plane.app.views.page.base.track_page_version.delay")


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Pages Project",
        identifier="PGS",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


def pages_url(slug, project_id, page_id=None):
    base = f"/api/workspaces/{slug}/projects/{project_id}/pages/"
    if page_id:
        return f"{base}{page_id}/"
    return base


def archive_url(slug, project_id, page_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/pages/{page_id}/archive/"


def access_url(slug, project_id, page_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/pages/{page_id}/access/"


def description_url(slug, project_id, page_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/pages/{page_id}/description/"


@pytest.mark.contract
@pytest.mark.django_db
class TestPageFolders:
    def test_missing_page_returns_404(self, session_client, workspace, project):
        response = session_client.get(pages_url(workspace.slug, project.id, uuid4()))
        # Object-level permission deliberately hides an unknown UUID before the
        # view is invoked. The regression assertion is that this is not a 500.
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_folder_and_nested_page(self, session_client, workspace, project, create_user):
        folder_res = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        assert folder_res.status_code == status.HTTP_201_CREATED
        folder_id = folder_res.data["id"]
        assert folder_res.data["node_type"] == "folder"

        page_res = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Items", "node_type": "page", "access": 0, "parent": folder_id},
            format="json",
        )
        assert page_res.status_code == status.HTTP_201_CREATED
        assert page_res.data["parent"] == folder_id

        root_list = session_client.get(pages_url(workspace.slug, project.id), {"parent": "root"})
        assert root_list.status_code == status.HTTP_200_OK
        root_ids = {row["id"] for row in root_list.data}
        assert folder_id in root_ids
        assert page_res.data["id"] not in root_ids

        child_list = session_client.get(pages_url(workspace.slug, project.id), {"parent": folder_id})
        assert child_list.status_code == status.HTTP_200_OK
        child_ids = {row["id"] for row in child_list.data}
        assert page_res.data["id"] in child_ids

    def test_reject_page_as_parent(self, session_client, workspace, project):
        page_res = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Doc", "node_type": "page", "access": 0},
            format="json",
        )
        assert page_res.status_code == status.HTTP_201_CREATED

        nested = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Child", "node_type": "page", "access": 0, "parent": page_res.data["id"]},
            format="json",
        )
        assert nested.status_code == status.HTTP_400_BAD_REQUEST

    def test_reject_folder_cycle(self, session_client, workspace, project):
        a = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "A", "node_type": "folder", "access": 0},
            format="json",
        )
        b = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "B", "node_type": "folder", "access": 0, "parent": a.data["id"]},
            format="json",
        )
        assert a.status_code == status.HTTP_201_CREATED
        assert b.status_code == status.HTTP_201_CREATED

        cycle = session_client.patch(
            pages_url(workspace.slug, project.id, a.data["id"]),
            {"parent": b.data["id"]},
            format="json",
        )
        assert cycle.status_code == status.HTTP_400_BAD_REQUEST

    def test_reject_duplicate_sibling_folder_name(self, session_client, workspace, project):
        first = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        assert first.status_code == status.HTTP_201_CREATED
        second = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        assert second.status_code == status.HTTP_400_BAD_REQUEST

    def test_reject_non_empty_folder_delete(self, session_client, workspace, project):
        folder = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        page = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Items", "node_type": "page", "access": 0, "parent": folder.data["id"]},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED
        assert page.status_code == status.HTTP_201_CREATED

        archive = session_client.post(archive_url(workspace.slug, project.id, folder.data["id"]))
        assert archive.status_code == status.HTTP_200_OK

        delete = session_client.delete(pages_url(workspace.slug, project.id, folder.data["id"]))
        assert delete.status_code == status.HTTP_400_BAD_REQUEST

    def test_reject_cross_project_parent(self, session_client, workspace, project, create_user):
        other = Project.objects.create(
            name="Other",
            identifier="OTH",
            workspace=workspace,
            created_by=create_user,
        )
        ProjectMember.objects.create(project=other, member=create_user, role=20, is_active=True)
        folder = Page.objects.create(
            name="foreign",
            workspace=workspace,
            owned_by=create_user,
            node_type=Page.FOLDER_NODE,
            access=0,
        )
        ProjectPage.objects.create(project=other, page=folder, workspace=workspace)

        nested = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Items", "node_type": "page", "access": 0, "parent": str(folder.id)},
            format="json",
        )
        assert nested.status_code == status.HTTP_400_BAD_REQUEST

    def test_folders_only_list(self, session_client, workspace, project):
        folder = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        page = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Items", "node_type": "page", "access": 0},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED
        assert page.status_code == status.HTTP_201_CREATED

        res = session_client.get(pages_url(workspace.slug, project.id), {"folders_only": "true"})
        assert res.status_code == status.HTTP_200_OK
        ids = {row["id"] for row in res.data}
        assert folder.data["id"] in ids
        assert page.data["id"] not in ids
        assert all(row["node_type"] == "folder" for row in res.data)

    def test_private_folder_cannot_be_used_by_another_member(self, session_client, workspace, project):
        folder = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "private", "node_type": "folder", "access": 1},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED

        suffix = uuid4().hex
        other = User.objects.create(email=f"other-{suffix}@plane.so", username=f"other-{suffix}")
        WorkspaceMember.objects.create(workspace=workspace, member=other, role=15, is_active=True)
        ProjectMember.objects.create(project=project, member=other, role=15, is_active=True)
        client = APIClient()
        client.force_authenticate(user=other)

        nested = client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Hidden child", "node_type": "page", "access": 1, "parent": folder.data["id"]},
            format="json",
        )
        assert nested.status_code == status.HTTP_400_BAD_REQUEST

    def test_folder_content_updates_are_rejected(self, session_client, workspace, project):
        folder = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED

        metadata_update = session_client.patch(
            pages_url(workspace.slug, project.id, folder.data["id"]),
            {"description_html": "<p>not allowed</p>"},
            format="json",
        )
        assert metadata_update.status_code == status.HTTP_400_BAD_REQUEST

        binary_update = session_client.patch(
            description_url(workspace.slug, project.id, folder.data["id"]),
            {"description_html": "<p>not allowed</p>"},
            format="json",
        )
        assert binary_update.status_code == status.HTTP_400_BAD_REQUEST

    def test_access_change_rejected_for_nested_page_and_non_empty_folder(self, session_client, workspace, project):
        folder = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "wiki", "node_type": "folder", "access": 0},
            format="json",
        )
        page = session_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Items", "node_type": "page", "access": 0, "parent": folder.data["id"]},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED
        assert page.status_code == status.HTTP_201_CREATED

        nested_access = session_client.post(
            access_url(workspace.slug, project.id, page.data["id"]),
            {"access": 1},
            format="json",
        )
        assert nested_access.status_code == status.HTTP_400_BAD_REQUEST

        folder_access = session_client.post(
            access_url(workspace.slug, project.id, folder.data["id"]),
            {"access": 1},
            format="json",
        )
        assert folder_access.status_code == status.HTTP_400_BAD_REQUEST

    def test_recorded_folder_migration_is_reversible(self, tmp_path, workspace, project, create_user):
        first = Page.objects.create(name="First", workspace=workspace, owned_by=create_user)
        second = Page.objects.create(name="Second", workspace=workspace, owned_by=create_user)
        ProjectPage.objects.create(project=project, page=first, workspace=workspace)
        ProjectPage.objects.create(project=project, page=second, workspace=workspace)
        mapping = {
            "version": "test-v1",
            "project_id": str(project.id),
            "folders": [
                {"key": "root", "name": "Docs", "parent": None, "sort_order": 0},
                {"key": "child", "name": "Product", "parent": "root", "sort_order": 10},
            ],
            "assignments": [
                {"page_id": str(first.id), "folder": "root", "sort_order": 1},
                {"page_id": str(second.id), "folder": "child", "sort_order": 2},
            ],
        }
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(json.dumps(mapping), encoding="utf-8")

        call_command(
            "migrate_page_folders",
            project=str(project.id),
            mapping_file=str(mapping_file),
            apply=True,
            stdout=StringIO(),
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.parent is not None and first.parent.name == "Docs"
        assert second.parent is not None and second.parent.name == "Product"
        assert second.parent.parent_id == first.parent_id

        call_command(
            "migrate_page_folders",
            project=str(project.id),
            mapping_file=str(mapping_file),
            restore=True,
            stdout=StringIO(),
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.parent_id is None
        assert second.parent_id is None
