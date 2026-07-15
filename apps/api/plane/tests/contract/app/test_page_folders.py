# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for project page folders (PokeBedrock fork)."""

from uuid import uuid4

import pytest
from rest_framework import status

from plane.db.models import Page, Project, ProjectMember, ProjectPage, Workspace, WorkspaceMember, User


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


@pytest.mark.contract
@pytest.mark.django_db
class TestPageFolders:
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
