from plane.license.models import InstanceConfiguration
from plane.license.utils.encryption import decrypt_data, encrypt_data
from plane.utils.confluence.client import ConfluenceError, validate_site_url

CONFIG_FIELDS = {
    "enabled": "JIRA_ENABLED",
    "site_url": "JIRA_SITE_URL",
    "email": "JIRA_EMAIL",
    "api_token": "JIRA_API_TOKEN",
    "cloud_id": "JIRA_CLOUD_ID",
}


def get_jira_config(*, require_enabled=False):
    rows = {row.key: row for row in InstanceConfiguration.objects.filter(key__in=CONFIG_FIELDS.values())}
    config = {}
    for field, key in CONFIG_FIELDS.items():
        row = rows.get(key)
        config[field] = ((decrypt_data(row.value) if row.is_encrypted else row.value) or "") if row else ""
    config["enabled"] = config["enabled"] == "1"
    if require_enabled and not (config["enabled"] and config["site_url"] and config["email"] and config["api_token"]):
        raise ConfluenceError("not_configured", "Configure and enable Jira in God Mode first.")
    return config


def public_config(config):
    return {
        **{key: value for key, value in config.items() if key != "api_token"},
        "token_configured": bool(config["api_token"]),
    }


def save_jira_config(data):
    from django.db import transaction
    from rest_framework.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError as DjangoValidationError
    import re

    if not isinstance(data, dict):
        raise ValidationError({"error": "Jira configuration must be an object."})
    if set(data) - set(CONFIG_FIELDS):
        raise ValidationError({"error": "Unknown Jira configuration field."})
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ValidationError({"enabled": "Use a boolean value."})
    for field in set(data) - {"enabled"}:
        if not isinstance(data[field], str):
            raise ValidationError({field: "Use a string value."})
    with transaction.atomic():
        # Serialize updates including the initial insertion of the five rows.
        for field, key in CONFIG_FIELDS.items():
            InstanceConfiguration.objects.get_or_create(
                key=key,
                defaults={
                    "value": "",
                    "category": "JIRA",
                    "is_encrypted": field == "api_token",
                },
            )
        list(InstanceConfiguration.objects.select_for_update().filter(key__in=CONFIG_FIELDS.values()).order_by("key"))
        config = {**get_jira_config(), **data}
        for field in set(CONFIG_FIELDS) - {"enabled"}:
            config[field] = config[field].strip()
        if config["site_url"]:
            config["site_url"] = validate_site_url(config["site_url"])
        if config["email"]:
            try:
                validate_email(config["email"])
            except DjangoValidationError:
                raise ValidationError({"email": "Enter the Atlassian account email."}) from None
        if config["cloud_id"] and not re.fullmatch(r"[a-fA-F0-9-]{36}", config["cloud_id"]):
            raise ValidationError({"cloud_id": "Enter a valid Atlassian Cloud ID UUID."})
        if config["enabled"] and not all(config[field] for field in ("site_url", "email", "api_token")):
            raise ValidationError({"error": "Site URL, account email and API token are required before enabling Jira."})
        for field, key in CONFIG_FIELDS.items():
            value = ("1" if config[field] else "0") if field == "enabled" else config[field].strip()
            stored = encrypt_data(value) if field == "api_token" else value
            if value and field == "api_token" and not stored:
                raise ValidationError({"api_token": "Could not encrypt the token; configuration was not saved."})
            InstanceConfiguration.objects.filter(key=key).update(value=stored, is_encrypted=field == "api_token")
    return public_config(config)
