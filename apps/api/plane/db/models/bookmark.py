# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import models

from .workspace import WorkspaceBaseModel


class WorkspaceBookmarkGroup(WorkspaceBaseModel):
    name = models.CharField(max_length=255)
    sort_order = models.FloatField(default=0)

    class Meta:
        verbose_name = "Workspace Bookmark Group"
        verbose_name_plural = "Workspace Bookmark Groups"
        db_table = "workspace_bookmark_groups"
        ordering = ("sort_order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="workspace_bookmark_group_unique_active_name",
            )
        ]

    def __str__(self):
        return f"{self.workspace_id} {self.name}"


class WorkspaceBookmark(WorkspaceBaseModel):
    title = models.CharField(max_length=255)
    url = models.TextField()
    remark = models.TextField(blank=True, default="")
    group = models.ForeignKey(
        WorkspaceBookmarkGroup,
        on_delete=models.SET_NULL,
        related_name="bookmarks",
        null=True,
        blank=True,
    )
    sort_order = models.FloatField(default=0)

    class Meta:
        verbose_name = "Workspace Bookmark"
        verbose_name_plural = "Workspace Bookmarks"
        db_table = "workspace_bookmarks"
        ordering = ("sort_order", "title")

    def __str__(self):
        return f"{self.workspace_id} {self.title}"
