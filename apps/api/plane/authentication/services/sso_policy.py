# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.conf import settings

from plane.license.models import InstanceAdmin, SSOProvider


def get_enforced_sso_provider():
    return SSOProvider.objects.filter(is_enabled=True, is_enforced=True).first()


def is_break_glass_email(email):
    normalized_email = str(email or "").strip().lower()
    return bool(normalized_email) and normalized_email in settings.SSO_BREAK_GLASS_ADMIN_EMAILS


def has_viable_break_glass_admin(provider):
    if not settings.SSO_BREAK_GLASS_ADMIN_EMAILS:
        return False
    return InstanceAdmin.objects.filter(
        instance=provider.instance,
        user__email__in=settings.SSO_BREAK_GLASS_ADMIN_EMAILS,
        user__is_active=True,
        user__is_bot=False,
    ).exists()
