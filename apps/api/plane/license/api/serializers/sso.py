# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from rest_framework import serializers
from django.conf import settings

# Module imports
from plane.license.models import SSOProvider
from plane.authentication.services import (
    has_normal_authentication_method,
    has_viable_break_glass_admin,
    is_sso_configuration_ready,
)

from .base import BaseSerializer


class SSOProviderSerializer(BaseSerializer):
    client_secret = serializers.CharField(write_only=True, required=False, allow_blank=False, trim_whitespace=False)
    client_secret_configured = serializers.BooleanField(read_only=True)
    configuration_ready = serializers.SerializerMethodField()
    recovery_ready = serializers.SerializerMethodField()
    mode = serializers.SerializerMethodField()
    deployment_enabled = serializers.SerializerMethodField()

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
            "configuration_ready",
            "recovery_ready",
            "mode",
            "deployment_enabled",
            "scopes",
            "claim_mappings",
            "allowed_email_domains",
            "allowed_groups",
            "jit_provisioning_enabled",
            "allow_verified_email_auto_link",
            "is_enabled",
            "is_enforced",
            "metadata_tested_at",
            "configuration_tested_at",
            "recovery_tested_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "metadata_tested_at",
            "configuration_tested_at",
            "recovery_tested_at",
            "created_at",
            "updated_at",
        ]

    def get_configuration_ready(self, obj):
        return is_sso_configuration_ready(obj)

    def get_recovery_ready(self, obj):
        return has_viable_break_glass_admin(obj)

    def get_mode(self, obj):
        if not settings.ENABLE_OIDC_SSO or not obj.is_enabled:
            return "disabled"
        return "enforced" if obj.is_enforced else "optional"

    def get_deployment_enabled(self, obj):
        return settings.ENABLE_OIDC_SSO

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
        supported_claims = {"email", "email_verified", "name", "given_name", "family_name", "groups"}
        unsupported = set(value) - supported_claims
        if unsupported:
            raise serializers.ValidationError(f"Unsupported claim mappings: {', '.join(sorted(unsupported))}.")
        if not all(isinstance(source, str) and source.strip() for source in value.values()):
            raise serializers.ValidationError("Claim mapping values must be non-empty strings.")
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
        connection_fields = {"issuer_url", "client_id", "scopes", "protocol", "claim_mappings"}
        connection_changed = bool(attrs.get("client_secret")) or any(
            field in attrs and (instance is None or attrs[field] != getattr(instance, field))
            for field in connection_fields
        )
        if connection_changed:
            tested_at = None
        if is_enforced and not is_enabled:
            raise serializers.ValidationError({"is_enforced": "An enforced provider must be enabled."})
        if is_enabled and not settings.ENABLE_OIDC_SSO:
            raise serializers.ValidationError({"is_enabled": "OIDC SSO is disabled for this deployment."})
        if is_enabled and not has_secret:
            raise serializers.ValidationError({"client_secret": "A client secret is required before enabling SSO."})
        if is_enabled and (tested_at is None or (instance is not None and not is_sso_configuration_ready(instance))):
            raise serializers.ValidationError(
                {"is_enabled": "Complete the interactive SSO login test before enabling it."}
            )
        if instance and instance.is_enabled and not is_enabled and not has_normal_authentication_method():
            raise serializers.ValidationError(
                {"is_enabled": "Enable at least one non-SSO authentication method before disabling SSO."}
            )
        if (
            is_enabled
            and SSOProvider.objects.filter(instance=instance.instance if instance else None, is_enabled=True)
            .exclude(pk=instance.pk if instance else None)
            .exists()
        ):
            raise serializers.ValidationError({"is_enabled": "Only one SSO provider can be enabled at a time."})
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
        connection_fields = {"issuer_url", "client_id", "scopes", "protocol", "claim_mappings"}
        connection_changed = client_secret is not None or any(
            field in validated_data and validated_data[field] != getattr(instance, field) for field in connection_fields
        )
        for attribute, value in validated_data.items():
            setattr(instance, attribute, value)
        if client_secret is not None:
            instance.set_client_secret(client_secret)
        if connection_changed:
            instance.configuration_tested_at = None
            instance.configuration_fingerprint = ""
            instance.recovery_tested_at = None
            instance.recovery_tested_by = None
        instance.save()
        return instance
