# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

# Django imports
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

# Module imports
from plane.db.models import BaseModel
from plane.db.mixins import TimeAuditModel
from plane.license.utils.encryption import decrypt_data, encrypt_data


class SSOProvider(BaseModel):
    """Instance-level SSO configuration with an encrypted client secret."""

    class Protocol(models.TextChoices):
        OIDC = "oidc", "OpenID Connect"

    instance = models.ForeignKey("license.Instance", on_delete=models.CASCADE, related_name="sso_providers")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100)
    protocol = models.CharField(max_length=20, choices=Protocol.choices, default=Protocol.OIDC)
    issuer_url = models.URLField(max_length=2048)
    client_id = models.CharField(max_length=512)
    client_secret_encrypted = models.TextField(blank=True, default="")
    scopes = models.JSONField(default=list)
    claim_mappings = models.JSONField(default=dict)
    allowed_email_domains = models.JSONField(default=list)
    allowed_groups = models.JSONField(default=list)
    jit_provisioning_enabled = models.BooleanField(default=False)
    allow_verified_email_auto_link = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=False)
    is_enforced = models.BooleanField(default=False)
    configuration_tested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "SSO Provider"
        verbose_name_plural = "SSO Providers"
        db_table = "sso_providers"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["instance", "slug"], name="unique_sso_provider_slug_per_instance"),
            models.UniqueConstraint(fields=["instance", "issuer_url"], name="unique_sso_provider_issuer_per_instance"),
        ]

    @property
    def client_secret_configured(self):
        return bool(self.client_secret_encrypted)

    def set_client_secret(self, value):
        encrypted_value = encrypt_data(value)
        if value and not encrypted_value:
            raise ValidationError("Could not encrypt the SSO client secret")
        self.client_secret_encrypted = encrypted_value

    def get_client_secret(self):
        return decrypt_data(self.client_secret_encrypted)


class SSOIdentity(BaseModel):
    """Stable link between an OIDC issuer subject and a Plane user."""

    provider = models.ForeignKey(SSOProvider, on_delete=models.PROTECT, related_name="identities")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sso_identities")
    subject = models.CharField(max_length=512)
    claims = models.JSONField(default=dict)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "SSO Identity"
        verbose_name_plural = "SSO Identities"
        db_table = "sso_identities"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="unique_sso_subject_per_provider"),
            models.UniqueConstraint(fields=["provider", "user"], name="unique_sso_user_per_provider"),
        ]


class SSOAuditEvent(TimeAuditModel):
    """Append-only security event for SSO administration and authentication."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(
        SSOProvider,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="sso_audit_events",
        null=True,
        blank=True,
    )
    event = models.CharField(max_length=100)
    outcome = models.CharField(max_length=20)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        verbose_name = "SSO Audit Event"
        verbose_name_plural = "SSO Audit Events"
        db_table = "sso_audit_events"
        ordering = ("-created_at",)
