# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from rest_framework import serializers

from plane.db.models import WorkspaceBookmark, WorkspaceBookmarkGroup

from .base import BaseSerializer


class WorkspaceBookmarkGroupSerializer(BaseSerializer):
    class Meta:
        model = WorkspaceBookmarkGroup
        fields = "__all__"
        read_only_fields = ["workspace", "project", "created_by", "updated_by"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Group name is required.")

        workspace = self.context.get("workspace")
        if workspace:
            duplicate_groups = WorkspaceBookmarkGroup.objects.filter(
                workspace=workspace,
                name__iexact=name,
            )
            if self.instance:
                duplicate_groups = duplicate_groups.exclude(pk=self.instance.pk)
            if duplicate_groups.exists():
                raise serializers.ValidationError("A group with this name already exists.")
        return name


class WorkspaceBookmarkSerializer(BaseSerializer):
    class Meta:
        model = WorkspaceBookmark
        fields = "__all__"
        read_only_fields = ["workspace", "project", "created_by", "updated_by"]

    def to_internal_value(self, data):
        mutable_data = data.copy()
        url = mutable_data.get("url", "")
        if url and not url.startswith(("http://", "https://")):
            mutable_data["url"] = f"https://{url}"
        return super().to_internal_value(mutable_data)

    def validate_title(self, value):
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Bookmark title is required.")
        return title

    def validate_url(self, value):
        try:
            URLValidator(schemes=["http", "https"])(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError("Invalid URL format.") from exc
        return value

    def validate_remark(self, value):
        return value.strip()

    def validate_group(self, value):
        workspace = self.context.get("workspace")
        if value and workspace and value.workspace_id != workspace.id:
            raise serializers.ValidationError("The group must belong to this workspace.")
        return value
