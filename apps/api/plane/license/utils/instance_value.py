# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import os

# Django imports
from django.conf import settings
from django.db import OperationalError, ProgrammingError

# Module imports
from plane.license.models import InstanceConfiguration
from plane.license.utils.encryption import decrypt_data


# Helper function to return value from the passed key
def get_configuration_value(keys):
    environment_list = []
    if settings.SKIP_ENV_VAR:
        # Get the configurations
        instance_configuration = InstanceConfiguration.objects.values("key", "value", "is_encrypted")

        for key in keys:
            for item in instance_configuration:
                if key.get("key") == item.get("key"):
                    if item.get("is_encrypted", False):
                        environment_list.append(decrypt_data(item.get("value")))
                    else:
                        environment_list.append(item.get("value"))

                    break
            else:
                environment_list.append(key.get("default"))
    else:
        # Get the configuration from os
        for key in keys:
            environment_list.append(os.environ.get(key.get("key"), key.get("default")))

    return tuple(environment_list)


def get_email_configuration():
    return get_configuration_value(
        [
            {"key": "EMAIL_HOST", "default": os.environ.get("EMAIL_HOST")},
            {"key": "EMAIL_HOST_USER", "default": os.environ.get("EMAIL_HOST_USER")},
            {
                "key": "EMAIL_HOST_PASSWORD",
                "default": os.environ.get("EMAIL_HOST_PASSWORD"),
            },
            {"key": "EMAIL_PORT", "default": os.environ.get("EMAIL_PORT", 587)},
            {"key": "EMAIL_USE_TLS", "default": os.environ.get("EMAIL_USE_TLS", "1")},
            {"key": "EMAIL_USE_SSL", "default": os.environ.get("EMAIL_USE_SSL", "0")},
            {
                "key": "EMAIL_FROM",
                "default": os.environ.get("EMAIL_FROM", "Team Plane <team@mailer.plane.so>"),
            },
        ]
    )


def get_storage_configuration():
    """Return object-storage settings with database configuration overriding env values."""
    defaults = {
        "STORAGE_PROVIDER": os.environ.get("STORAGE_PROVIDER", "s3"),
        "CLOUDFLARE_R2_ACCOUNT_ID": os.environ.get("CLOUDFLARE_R2_ACCOUNT_ID", ""),
        "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
        "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        "AWS_S3_BUCKET_NAME": os.environ.get("AWS_S3_BUCKET_NAME", "uploads"),
        "AWS_S3_ENDPOINT_URL": os.environ.get("AWS_S3_ENDPOINT_URL")
        or os.environ.get("MINIO_ENDPOINT_URL", ""),
        "AWS_S3_REGION_NAME": os.environ.get("AWS_S3_REGION_NAME") or os.environ.get("AWS_REGION", ""),
        "AWS_S3_ADDRESSING_STYLE": os.environ.get("AWS_S3_ADDRESSING_STYLE", "auto"),
        "AWS_S3_SIGNATURE_VERSION": os.environ.get("AWS_S3_SIGNATURE_VERSION", "s3v4"),
        "SIGNED_URL_EXPIRATION": os.environ.get("SIGNED_URL_EXPIRATION", "3600"),
    }
    keys = [{"key": key, "default": value} for key, value in defaults.items()]

    try:
        values = dict(zip(defaults, get_configuration_value(keys), strict=True))
    except (OperationalError, ProgrammingError, RuntimeError):
        # Storage is also constructed by management commands before the
        # instance-configuration table is available.
        values = defaults

    provider = str(values["STORAGE_PROVIDER"] or "s3").strip().lower()
    endpoint_url = str(values["AWS_S3_ENDPOINT_URL"] or "").strip()
    account_id = str(values["CLOUDFLARE_R2_ACCOUNT_ID"] or "").strip()
    if provider == "r2" and not endpoint_url and account_id:
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    try:
        signed_url_expiration = max(1, int(values["SIGNED_URL_EXPIRATION"] or 3600))
    except (TypeError, ValueError):
        signed_url_expiration = 3600

    return {
        "provider": provider,
        "access_key_id": str(values["AWS_ACCESS_KEY_ID"] or "").strip(),
        "secret_access_key": str(values["AWS_SECRET_ACCESS_KEY"] or "").strip(),
        "bucket_name": str(values["AWS_S3_BUCKET_NAME"] or "uploads").strip(),
        "endpoint_url": endpoint_url or None,
        "region_name": str(values["AWS_S3_REGION_NAME"] or ("auto" if provider == "r2" else "")).strip(),
        "addressing_style": str(values["AWS_S3_ADDRESSING_STYLE"] or "auto").strip(),
        "signature_version": str(values["AWS_S3_SIGNATURE_VERSION"] or "s3v4").strip(),
        "signed_url_expiration": signed_url_expiration,
    }
