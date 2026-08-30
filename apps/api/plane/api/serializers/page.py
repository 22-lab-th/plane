# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from rest_framework import serializers

from plane.api.serializers.base import BaseSerializer
from plane.db.models import Page
from plane.utils.content_validator import validate_html_content


class PublicPageSerializer(BaseSerializer):
    """API-token representation of a Plane page."""

    name = serializers.CharField(required=True, allow_blank=False)
    description_html = serializers.CharField(required=False, allow_blank=True, default="<p></p>")
    projects = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = Page
        fields = [
            "id",
            "name",
            "description_stripped",
            "description_html",
            "created_at",
            "updated_at",
            "owned_by",
            "workspace",
            "projects",
            "parent",
            "node_type",
            "sort_order",
            "access",
            "color",
            "is_locked",
            "archived_at",
            "view_props",
            "logo_props",
            "external_id",
            "external_source",
        ]
        read_only_fields = [
            "id",
            "description_stripped",
            "created_at",
            "updated_at",
            "owned_by",
            "workspace",
            "projects",
        ]

    def validate_description_html(self, value):
        if not value:
            return value
        is_valid, error_message, sanitized_html = validate_html_content(value)
        if not is_valid:
            raise serializers.ValidationError(error_message)
        return sanitized_html if sanitized_html is not None else value

    def validate(self, attrs):
        if attrs.get("node_type") == Page.FOLDER_NODE and attrs.get("description_html") not in (
            None,
            "",
            "<p></p>",
        ):
            raise serializers.ValidationError({"description_html": "Folders cannot contain document content"})
        external_id = attrs.get("external_id")
        external_source = attrs.get("external_source")
        if bool(external_id) != bool(external_source):
            raise serializers.ValidationError(
                {"external_id": "external_id and external_source must be provided together"}
            )
        return attrs
