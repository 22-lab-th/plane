# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.urls import reverse
from rest_framework import status

from plane.db.models import User, Workspace, WorkspaceBookmark, WorkspaceBookmarkGroup, WorkspaceMember


@pytest.mark.contract
@pytest.mark.django_db
class TestWorkspaceBookmarks:
    def test_bookmarks_are_shared_and_keep_remarks(self, session_client, workspace):
        groups_url = reverse("workspace-bookmark-groups", kwargs={"slug": workspace.slug})
        group_response = session_client.post(groups_url, {"name": "Engineering"}, format="json")
        assert group_response.status_code == status.HTTP_201_CREATED

        bookmarks_url = reverse("workspace-bookmarks", kwargs={"slug": workspace.slug})
        bookmark_response = session_client.post(
            bookmarks_url,
            {
                "title": "Runbook",
                "url": "docs.example.com/runbook",
                "remark": "Use this during production incidents.",
                "group": group_response.data["id"],
            },
            format="json",
        )
        assert bookmark_response.status_code == status.HTTP_201_CREATED
        assert bookmark_response.data["url"] == "https://docs.example.com/runbook"
        assert bookmark_response.data["remark"] == "Use this during production incidents."

        member = User.objects.create_user(email="bookmark-member@example.com", username="bookmark-member")
        WorkspaceMember.objects.create(workspace=workspace, member=member, role=15)
        session_client.force_authenticate(user=member)

        list_response = session_client.get(bookmarks_url)
        assert list_response.status_code == status.HTTP_200_OK
        assert [item["title"] for item in list_response.data] == ["Runbook"]

    def test_guest_can_read_but_cannot_manage(self, session_client, workspace):
        WorkspaceBookmark.objects.create(workspace=workspace, title="Shared", url="https://example.com")
        guest = User.objects.create_user(email="bookmark-guest@example.com", username="bookmark-guest")
        WorkspaceMember.objects.create(workspace=workspace, member=guest, role=5)
        session_client.force_authenticate(user=guest)

        bookmarks_url = reverse("workspace-bookmarks", kwargs={"slug": workspace.slug})
        assert session_client.get(bookmarks_url).status_code == status.HTTP_200_OK
        create_response = session_client.post(
            bookmarks_url,
            {"title": "Forbidden", "url": "https://example.org"},
            format="json",
        )
        assert create_response.status_code == status.HTTP_403_FORBIDDEN

    def test_group_must_belong_to_same_workspace(self, session_client, create_user, workspace):
        other_workspace = Workspace.objects.create(
            name="Other Workspace",
            slug="other-workspace-bookmarks",
            owner=create_user,
        )
        other_group = WorkspaceBookmarkGroup.objects.create(workspace=other_workspace, name="Other")

        response = session_client.post(
            reverse("workspace-bookmarks", kwargs={"slug": workspace.slug}),
            {"title": "Invalid", "url": "https://example.com", "group": str(other_group.id)},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "group" in response.data

    def test_group_names_are_case_insensitively_unique(self, session_client, workspace):
        groups_url = reverse("workspace-bookmark-groups", kwargs={"slug": workspace.slug})
        assert session_client.post(groups_url, {"name": "Design"}, format="json").status_code == status.HTTP_201_CREATED
        duplicate = session_client.post(groups_url, {"name": " design "}, format="json")
        assert duplicate.status_code == status.HTTP_400_BAD_REQUEST
        assert "name" in duplicate.data

    def test_deleting_group_moves_bookmarks_to_ungrouped(self, session_client, workspace):
        group = WorkspaceBookmarkGroup.objects.create(workspace=workspace, name="Temporary")
        bookmark = WorkspaceBookmark.objects.create(
            workspace=workspace,
            group=group,
            title="Keep me",
            url="https://example.com/keep",
        )

        detail_url = reverse(
            "workspace-bookmark-groups",
            kwargs={"slug": workspace.slug, "pk": group.id},
        )
        assert session_client.delete(detail_url).status_code == status.HTTP_204_NO_CONTENT

        bookmark.refresh_from_db()
        assert bookmark.group_id is None
        assert not WorkspaceBookmarkGroup.objects.filter(pk=group.id).exists()
