import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def migrate_metadata_test_timestamp(apps, schema_editor):
    SSOProvider = apps.get_model("license", "SSOProvider")
    SSOProvider.objects.filter(configuration_tested_at__isnull=False).update(
        metadata_tested_at=models.F("configuration_tested_at"),
        configuration_tested_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("license", "0008_ssoauditevent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ssoauditevent",
            name="correlation_id",
            field=models.CharField(db_index=True, default="migration", max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="ssoprovider",
            name="metadata_tested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ssoprovider",
            name="configuration_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="ssoprovider",
            name="recovery_tested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ssoprovider",
            name="recovery_tested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="recovery_tested_sso_providers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(migrate_metadata_test_timestamp, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="ssoprovider",
            constraint=models.UniqueConstraint(
                condition=Q(is_enabled=True),
                fields=("instance",),
                name="unique_enabled_sso_provider_per_instance",
            ),
        ),
        migrations.AddConstraint(
            model_name="ssoprovider",
            constraint=models.UniqueConstraint(
                condition=Q(is_enforced=True),
                fields=("instance",),
                name="unique_enforced_sso_provider_per_instance",
            ),
        ),
    ]
