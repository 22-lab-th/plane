from django.db import migrations


def seed_config(apps, schema_editor):
    configuration = apps.get_model("license", "InstanceConfiguration")
    for key, value in {
        "JIRA_ENABLED": "0",
        "JIRA_SITE_URL": "",
        "JIRA_EMAIL": "",
        "JIRA_API_TOKEN": "",
        "JIRA_CLOUD_ID": "",
    }.items():
        configuration.objects.get_or_create(
            key=key, defaults={"value": value, "category": "JIRA", "is_encrypted": key == "JIRA_API_TOKEN"}
        )


class Migration(migrations.Migration):
    dependencies = [("license", "0010_confluence_configuration")]
    operations = [migrations.RunPython(seed_config, migrations.RunPython.noop)]
