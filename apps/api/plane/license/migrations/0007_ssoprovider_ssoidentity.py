# Generated manually for the structured SSO provider foundation.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("license", "0006_instance_is_current_version_deprecated"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SSOProvider",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Last Modified At")),
                ("deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="Deleted At")),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=100)),
                (
                    "protocol",
                    models.CharField(choices=[("oidc", "OpenID Connect")], default="oidc", max_length=20),
                ),
                ("issuer_url", models.URLField(max_length=2048)),
                ("client_id", models.CharField(max_length=512)),
                ("client_secret_encrypted", models.TextField(blank=True, default="")),
                ("scopes", models.JSONField(default=list)),
                ("claim_mappings", models.JSONField(default=dict)),
                ("allowed_email_domains", models.JSONField(default=list)),
                ("allowed_groups", models.JSONField(default=list)),
                ("jit_provisioning_enabled", models.BooleanField(default=False)),
                ("allow_verified_email_auto_link", models.BooleanField(default=False)),
                ("is_enabled", models.BooleanField(default=False)),
                ("is_enforced", models.BooleanField(default=False)),
                ("configuration_tested_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "instance",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sso_providers",
                        to="license.instance",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
            ],
            options={
                "verbose_name": "SSO Provider",
                "verbose_name_plural": "SSO Providers",
                "db_table": "sso_providers",
                "ordering": ("name",),
            },
        ),
        migrations.CreateModel(
            name="SSOIdentity",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created At")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Last Modified At")),
                ("deleted_at", models.DateTimeField(blank=True, null=True, verbose_name="Deleted At")),
                (
                    "id",
                    models.UUIDField(
                        db_index=True,
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                        unique=True,
                    ),
                ),
                ("subject", models.CharField(max_length=512)),
                ("claims", models.JSONField(default=dict)),
                ("last_login_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_created_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created By",
                    ),
                ),
                (
                    "provider",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="identities",
                        to="license.ssoprovider",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(class)s_updated_by",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last Modified By",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sso_identities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "SSO Identity",
                "verbose_name_plural": "SSO Identities",
                "db_table": "sso_identities",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="ssoprovider",
            constraint=models.UniqueConstraint(
                fields=("instance", "slug"), name="unique_sso_provider_slug_per_instance"
            ),
        ),
        migrations.AddConstraint(
            model_name="ssoprovider",
            constraint=models.UniqueConstraint(
                fields=("instance", "issuer_url"), name="unique_sso_provider_issuer_per_instance"
            ),
        ),
        migrations.AddConstraint(
            model_name="ssoidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "subject"), name="unique_sso_subject_per_provider"
            ),
        ),
        migrations.AddConstraint(
            model_name="ssoidentity",
            constraint=models.UniqueConstraint(
                fields=("provider", "user"), name="unique_sso_user_per_provider"
            ),
        ),
    ]
