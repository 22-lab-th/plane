from django.db import migrations


def seed_config(apps, schema_editor):
    configuration = apps.get_model("license", "InstanceConfiguration")
    for key, value in {
        "CONFLUENCE_ENABLED": "0",
        "CONFLUENCE_SITE_URL": "",
        "CONFLUENCE_EMAIL": "",
        "CONFLUENCE_API_TOKEN": "",
        "CONFLUENCE_CLOUD_ID": "",
    }.items():
        configuration.objects.get_or_create(
            key=key, defaults={"value": value, "category": "CONFLUENCE", "is_encrypted": key == "CONFLUENCE_API_TOKEN"}
        )


class Migration(migrations.Migration):
    dependencies = [("license", "0009_sso_provider_readiness")]
    operations = [migrations.RunPython(seed_config, migrations.RunPython.noop)]
