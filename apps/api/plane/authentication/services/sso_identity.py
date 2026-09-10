# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.db.models import Profile, User
from plane.license.models import SSOIdentity

SAFE_STORED_CLAIMS = {"email", "email_verified", "name", "given_name", "family_name", "groups"}


class SSOIdentityError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SSOIdentityResolver:
    def __init__(self, provider):
        self.provider = provider

    def _claim(self, claims, name):
        source_name = self.provider.claim_mappings.get(name, name)
        return claims.get(source_name)

    def _safe_claims(self, claims):
        return {name: self._claim(claims, name) for name in SAFE_STORED_CLAIMS if self._claim(claims, name) is not None}

    def _validate_user(self, user):
        if not user.is_active:
            raise SSOIdentityError("inactive_user", "This Plane account is inactive.")
        if user.is_bot:
            raise SSOIdentityError("bot_user", "Service accounts cannot use interactive SSO.")

    def _validate_first_login_claims(self, claims):
        email = self._claim(claims, "email")
        email_verified = self._claim(claims, "email_verified")
        if not isinstance(email, str) or email_verified is not True:
            raise SSOIdentityError("unverified_email", "A verified email claim is required for first SSO login.")
        email = email.strip().lower()
        try:
            validate_email(email)
        except ValidationError as exc:
            raise SSOIdentityError("invalid_email", "The SSO provider returned an invalid email.") from exc
        domain = email.rsplit("@", 1)[1]
        if self.provider.allowed_email_domains and domain not in self.provider.allowed_email_domains:
            raise SSOIdentityError("domain_denied", "The email domain is not allowed for this SSO provider.")
        groups = self._claim(claims, "groups") or []
        if isinstance(groups, str):
            groups = [groups]
        normalized_groups = {group.strip().lower() for group in groups if isinstance(group, str) and group.strip()}
        if self.provider.allowed_groups and not normalized_groups.intersection(self.provider.allowed_groups):
            raise SSOIdentityError("group_denied", "The account is not in an allowed SSO group.")
        return email

    def _resolve_once(self, claims):
        subject = claims["sub"]
        safe_claims = self._safe_claims(claims)
        with transaction.atomic():
            identity = (
                SSOIdentity.objects.select_for_update()
                .select_related("user")
                .filter(provider=self.provider, subject=subject)
                .first()
            )
            if identity:
                self._validate_user(identity.user)
                identity.claims = safe_claims
                identity.last_login_at = timezone.now()
                identity.save(update_fields=["claims", "last_login_at", "updated_at"])
                return identity.user, False

            email = self._validate_first_login_claims(claims)
            user = User.objects.select_for_update().filter(email=email).first()
            is_signup = user is None
            if user is not None:
                self._validate_user(user)
                if not self.provider.allow_verified_email_auto_link:
                    raise SSOIdentityError(
                        "link_not_allowed", "An account exists for this email but automatic SSO linking is disabled."
                    )
            else:
                if not self.provider.jit_provisioning_enabled:
                    raise SSOIdentityError("jit_disabled", "No linked Plane account exists for this SSO identity.")
                user = User(
                    email=email,
                    username=uuid.uuid4().hex,
                    first_name=str(self._claim(claims, "given_name") or "")[:255],
                    last_name=str(self._claim(claims, "family_name") or "")[:255],
                    display_name=str(self._claim(claims, "name") or "")[:255],
                    is_email_verified=True,
                    is_password_autoset=True,
                )
                user.set_unusable_password()
                user.save()
                Profile.objects.create(user=user)

            SSOIdentity.objects.create(provider=self.provider, user=user, subject=subject, claims=safe_claims)
            return user, is_signup

    def resolve(self, claims):
        try:
            return self._resolve_once(claims)
        except IntegrityError as exc:
            identity = (
                SSOIdentity.objects.select_related("user").filter(provider=self.provider, subject=claims["sub"]).first()
            )
            if identity:
                self._validate_user(identity.user)
                return identity.user, False
            raise SSOIdentityError("identity_conflict", "The SSO identity could not be linked safely.") from exc
