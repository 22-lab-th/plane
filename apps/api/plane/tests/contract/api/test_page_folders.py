# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Public API contract tests for project Page folders."""

import pytest
from rest_framework import status

from plane.db.models import Project, ProjectMember


@pytest.fixture(autouse=True)
def mock_page_background_tasks(mocker):
    mocker.patch("plane.api.views.page.page_transaction.delay")


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Public Pages Project",
        identifier="PUB",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(
        workspace=workspace,
        project=project,
        member=create_user,
        role=20,
        is_active=True,
    )
    return project


def pages_url(slug, project_id, page_id=None):
    base = f"/api/v1/workspaces/{slug}/projects/{project_id}/pages/"
    return f"{base}{page_id}/" if page_id else base


def archive_url(slug, project_id, page_id):
    return f"{pages_url(slug, project_id, page_id)}archive/"


@pytest.mark.contract
@pytest.mark.django_db
class TestPublicPageFolders:
    def test_create_list_move_archive_and_restore(self, api_key_client, workspace, project):
        folder = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Delivery", "node_type": "folder", "access": 0},
            format="json",
        )
        assert folder.status_code == status.HTTP_201_CREATED
        assert folder.data["node_type"] == "folder"

        page = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {
                "name": "Runbook",
                "description_html": "<p>Deploy safely.</p>",
                "node_type": "page",
                "access": 0,
            },
            format="json",
        )
        assert page.status_code == status.HTTP_201_CREATED

        moved = api_key_client.patch(
            pages_url(workspace.slug, project.id, page.data["id"]),
            {"parent": folder.data["id"], "sort_order": 10},
            format="json",
        )
        assert moved.status_code == status.HTTP_200_OK
        assert moved.data["parent"] == folder.data["id"]

        children = api_key_client.get(pages_url(workspace.slug, project.id), {"parent": folder.data["id"]})
        assert children.status_code == status.HTTP_200_OK
        assert [row["id"] for row in children.data["results"]] == [page.data["id"]]

        archived = api_key_client.post(archive_url(workspace.slug, project.id, folder.data["id"]))
        assert archived.status_code == status.HTTP_200_OK

        restored = api_key_client.delete(archive_url(workspace.slug, project.id, folder.data["id"]))
        assert restored.status_code == status.HTTP_204_NO_CONTENT

    def test_reject_folder_cycle(self, api_key_client, workspace, project):
        first = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "First", "node_type": "folder", "access": 0},
            format="json",
        )
        second = api_key_client.post(
            pages_url(workspace.slug, project.id),
            {"name": "Second", "node_type": "folder", "access": 0, "parent": first.data["id"]},
            format="json",
        )
        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_201_CREATED

        cycle = api_key_client.patch(
            pages_url(workspace.slug, project.id, first.data["id"]),
            {"parent": second.data["id"]},
            format="json",
        )
        assert cycle.status_code == status.HTTP_400_BAD_REQUEST
