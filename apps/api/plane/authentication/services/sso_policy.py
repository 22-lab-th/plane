# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.conf import settings
from django.utils import timezone

from plane.license.models import InstanceAdmin, SSOProvider
from plane.authentication.services.sso_readiness import is_sso_configuration_ready


def get_enforced_sso_provider():
    if not settings.ENABLE_OIDC_SSO:
        return None
    provider = SSOProvider.objects.filter(is_enabled=True, is_enforced=True).first()
    return provider if provider and is_sso_configuration_ready(provider) else None


def is_break_glass_email(email):
    normalized_email = str(email or "").strip().lower()
    return bool(normalized_email) and normalized_email in settings.SSO_BREAK_GLASS_ADMIN_EMAILS


def has_viable_break_glass_admin(provider):
    if not settings.SSO_BREAK_GLASS_ADMIN_EMAILS or not provider.recovery_tested_at:
        return False
    if (timezone.now() - provider.recovery_tested_at).total_seconds() > settings.SSO_RECOVERY_TEST_MAX_AGE_SECONDS:
        return False
    admins = InstanceAdmin.objects.select_related("user").filter(
        instance=provider.instance,
        is_verified=True,
        user__email__in=settings.SSO_BREAK_GLASS_ADMIN_EMAILS,
        user__is_active=True,
        user__is_bot=False,
        user__is_email_verified=True,
    )
    return any(admin.user_id == provider.recovery_tested_by_id and admin.user.has_usable_password() for admin in admins)
