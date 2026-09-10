# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from plane.api.serializers.base import BaseSerializer
from plane.db.models import IssueView


class PublicIssueViewSerializer(BaseSerializer):
    """API-token representation of a project view."""

    class Meta:
        model = IssueView
        fields = [
            "id",
            "name",
            "description",
            "query",
            "filters",
            "display_filters",
            "display_properties",
            "rich_filters",
            "access",
            "sort_order",
            "logo_props",
            "owned_by",
            "is_locked",
            "archived_at",
            "workspace",
            "project",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "query",
            "access",
            "owned_by",
            "is_locked",
            "workspace",
            "project",
            "created_at",
            "updated_at",
        ]
