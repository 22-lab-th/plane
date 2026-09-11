# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from enum import StrEnum

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from plane.license.models import Instance, SSOProvider

from .sso_policy import has_viable_break_glass_admin
from .sso_readiness import has_normal_authentication_method, is_sso_configuration_ready


class SSOProviderMode(StrEnum):
    DISABLED = "disabled"
    OPTIONAL = "optional"
    ENFORCED = "enforced"


def get_sso_provider_mode(provider):
    if not settings.ENABLE_OIDC_SSO or not provider.is_enabled:
        return SSOProviderMode.DISABLED
    return SSOProviderMode.ENFORCED if provider.is_enforced else SSOProviderMode.OPTIONAL


class SSOProviderLifecycle:
    """Transactional authority for provider configuration and mode changes."""

    CONNECTION_FIELDS = {"issuer_url", "client_id", "scopes", "protocol", "claim_mappings"}

    @classmethod
    def update(cls, provider, validated_data):
        client_secret = validated_data.pop("client_secret", None)
        with transaction.atomic():
            # Serialize transitions per instance in addition to the database
            # constraints that enforce the final invariant.
            Instance.objects.select_for_update().get(pk=provider.instance_id)
            provider = SSOProvider.objects.select_for_update().get(pk=provider.pk)
            was_enabled = provider.is_enabled
            was_enforced = provider.is_enforced
            connection_changed = client_secret is not None or any(
                field in validated_data and validated_data[field] != getattr(provider, field)
                for field in cls.CONNECTION_FIELDS
            )
            for attribute, value in validated_data.items():
                setattr(provider, attribute, value)
            if client_secret is not None:
                provider.set_client_secret(client_secret)
            if connection_changed:
                provider.configuration_tested_at = None
                provider.configuration_fingerprint = ""
                provider.recovery_tested_at = None
                provider.recovery_tested_by = None

            cls._validate(
                provider,
                was_enabled=was_enabled,
                was_enforced=was_enforced,
                connection_changed=connection_changed,
            )
            provider.save()
            return provider

    @staticmethod
    def _validate(provider, was_enabled, was_enforced, connection_changed):
        if provider.is_enforced and not provider.is_enabled:
            raise ValidationError({"is_enforced": "An enforced provider must be enabled."})
        if provider.is_enabled and not settings.ENABLE_OIDC_SSO:
            raise ValidationError({"is_enabled": "OIDC SSO is disabled for this deployment."})
        if provider.is_enabled and not provider.client_secret_configured:
            raise ValidationError({"client_secret": "A client secret is required before enabling SSO."})
        readiness_required = provider.is_enabled and (
            not was_enabled or connection_changed or (provider.is_enforced and not was_enforced)
        )
        if readiness_required and not is_sso_configuration_ready(provider):
            raise ValidationError({"is_enabled": "Complete the interactive SSO login test before enabling it."})
        if was_enabled and not provider.is_enabled and not has_normal_authentication_method():
            raise ValidationError(
                {"is_enabled": "Enable at least one non-SSO authentication method before disabling SSO."}
            )
        if (
            provider.is_enabled
            and SSOProvider.objects.filter(instance_id=provider.instance_id, is_enabled=True)
            .exclude(pk=provider.pk)
            .exists()
        ):
            raise ValidationError({"is_enabled": "Only one SSO provider can be enabled at a time."})
        if provider.is_enforced and not has_viable_break_glass_admin(provider):
            raise ValidationError(
                {"is_enforced": "Configure an active break-glass instance administrator before enforcing SSO."}
            )
