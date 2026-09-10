# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers

# Module imports
from plane.license.models import SSOProvider
from plane.authentication.services.sso_policy import has_viable_break_glass_admin

from .base import BaseSerializer


class SSOProviderSerializer(BaseSerializer):
    client_secret = serializers.CharField(write_only=True, required=False, allow_blank=False, trim_whitespace=False)
    client_secret_configured = serializers.BooleanField(read_only=True)

    class Meta:
        model = SSOProvider
        fields = [
            "id",
            "name",
            "slug",
            "protocol",
            "issuer_url",
            "client_id",
            "client_secret",
            "client_secret_configured",
            "scopes",
            "claim_mappings",
            "allowed_email_domains",
            "allowed_groups",
            "jit_provisioning_enabled",
            "allow_verified_email_auto_link",
            "is_enabled",
            "is_enforced",
            "configuration_tested_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["configuration_tested_at", "created_at", "updated_at"]

    def validate_scopes(self, value):
        if not isinstance(value, list) or not all(isinstance(scope, str) and scope.strip() for scope in value):
            raise serializers.ValidationError("Scopes must be a list of non-empty strings.")
        required_scopes = {"openid", "email"}
        if not required_scopes.issubset(value):
            raise serializers.ValidationError("Scopes must include openid and email.")
        return list(dict.fromkeys(value))

    def validate_issuer_url(self, value):
        return value.rstrip("/")

    def validate_claim_mappings(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Claim mappings must be an object.")
        return value

    def _validate_string_list(self, value, field_name):
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise serializers.ValidationError(f"{field_name} must be a list of non-empty strings.")
        return list(dict.fromkeys(item.strip().lower() for item in value))

    def validate_allowed_email_domains(self, value):
        return self._validate_string_list(value, "Allowed email domains")

    def validate_allowed_groups(self, value):
        return self._validate_string_list(value, "Allowed groups")

    def validate(self, attrs):
        instance = self.instance
        is_enabled = attrs.get("is_enabled", instance.is_enabled if instance else False)
        is_enforced = attrs.get("is_enforced", instance.is_enforced if instance else False)
        has_secret = bool(attrs.get("client_secret")) or bool(instance and instance.client_secret_configured)
        tested_at = instance.configuration_tested_at if instance else None
        connection_fields = {"issuer_url", "client_id", "scopes", "protocol"}
        connection_changed = bool(attrs.get("client_secret")) or any(
            field in attrs and (instance is None or attrs[field] != getattr(instance, field))
            for field in connection_fields
        )
        if connection_changed:
            tested_at = None
        if is_enforced and not is_enabled:
            raise serializers.ValidationError({"is_enforced": "An enforced provider must be enabled."})
        if is_enabled and not has_secret:
            raise serializers.ValidationError({"client_secret": "A client secret is required before enabling SSO."})
        if is_enabled and tested_at is None:
            raise serializers.ValidationError({"is_enabled": "Test the SSO configuration before enabling it."})
        if is_enforced and (instance is None or not has_viable_break_glass_admin(instance)):
            raise serializers.ValidationError(
                {"is_enforced": "Configure an active break-glass instance administrator before enforcing SSO."}
            )
        return attrs

    def create(self, validated_data):
        client_secret = validated_data.pop("client_secret", "")
        provider = SSOProvider(**validated_data)
        provider.set_client_secret(client_secret)
        provider.save()
        return provider

    def update(self, instance, validated_data):
        client_secret = validated_data.pop("client_secret", None)
        connection_fields = {"issuer_url", "client_id", "scopes", "protocol"}
        connection_changed = client_secret is not None or any(
            field in validated_data and validated_data[field] != getattr(instance, field) for field in connection_fields
        )
        for attribute, value in validated_data.items():
            setattr(instance, attribute, value)
        if client_secret is not None:
            instance.set_client_secret(client_secret)
        if connection_changed:
            instance.configuration_tested_at = None
        instance.save()
        return instance
