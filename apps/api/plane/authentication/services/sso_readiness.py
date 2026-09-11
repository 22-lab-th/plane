# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
import os

from django.conf import settings

from plane.license.utils.instance_value import get_configuration_value


def get_sso_configuration_fingerprint(provider):
    payload = {
        "protocol": provider.protocol,
        "issuer_url": provider.issuer_url,
        "client_id": provider.client_id,
        "client_secret_encrypted": provider.client_secret_encrypted,
        "scopes": provider.scopes,
        "claim_mappings": provider.claim_mappings,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()


def is_sso_configuration_ready(provider):
    return bool(
        provider.configuration_tested_at
        and provider.configuration_fingerprint
        and provider.configuration_fingerprint == get_sso_configuration_fingerprint(provider)
    )


def has_normal_authentication_method():
    (
        email_password,
        magic_link,
        email_host,
        google,
        github,
        gitlab,
        gitea,
        google_client_id,
        google_client_secret,
        github_client_id,
        github_client_secret,
        gitlab_client_id,
        gitlab_client_secret,
        gitlab_host,
        gitea_client_id,
        gitea_client_secret,
        gitea_host,
    ) = get_configuration_value(
        [
            {"key": "ENABLE_EMAIL_PASSWORD", "default": os.environ.get("ENABLE_EMAIL_PASSWORD", "1")},
            {"key": "ENABLE_MAGIC_LINK_LOGIN", "default": os.environ.get("ENABLE_MAGIC_LINK_LOGIN", "1")},
            {"key": "EMAIL_HOST", "default": os.environ.get("EMAIL_HOST", "")},
            {"key": "IS_GOOGLE_ENABLED", "default": os.environ.get("IS_GOOGLE_ENABLED", "0")},
            {"key": "IS_GITHUB_ENABLED", "default": os.environ.get("IS_GITHUB_ENABLED", "0")},
            {"key": "IS_GITLAB_ENABLED", "default": os.environ.get("IS_GITLAB_ENABLED", "0")},
            {"key": "IS_GITEA_ENABLED", "default": os.environ.get("IS_GITEA_ENABLED", "0")},
            {"key": "GOOGLE_CLIENT_ID", "default": os.environ.get("GOOGLE_CLIENT_ID")},
            {"key": "GOOGLE_CLIENT_SECRET", "default": os.environ.get("GOOGLE_CLIENT_SECRET")},
            {"key": "GITHUB_CLIENT_ID", "default": os.environ.get("GITHUB_CLIENT_ID")},
            {"key": "GITHUB_CLIENT_SECRET", "default": os.environ.get("GITHUB_CLIENT_SECRET")},
            {"key": "GITLAB_CLIENT_ID", "default": os.environ.get("GITLAB_CLIENT_ID")},
            {"key": "GITLAB_CLIENT_SECRET", "default": os.environ.get("GITLAB_CLIENT_SECRET")},
            {"key": "GITLAB_HOST", "default": os.environ.get("GITLAB_HOST", "https://gitlab.com")},
            {"key": "GITEA_CLIENT_ID", "default": os.environ.get("GITEA_CLIENT_ID")},
            {"key": "GITEA_CLIENT_SECRET", "default": os.environ.get("GITEA_CLIENT_SECRET")},
            {"key": "GITEA_HOST", "default": os.environ.get("GITEA_HOST")},
        ]
    )
    oauth_ready = any(
        enabled == "1" and bool(client_id) and bool(client_secret) and (host is None or bool(host))
        for enabled, client_id, client_secret, host in (
            (google, google_client_id, google_client_secret, None),
            (github, github_client_id, github_client_secret, None),
            (gitlab, gitlab_client_id, gitlab_client_secret, gitlab_host),
            (gitea, gitea_client_id, gitea_client_secret, gitea_host),
        )
    )
    return email_password == "1" or (magic_link == "1" and bool(email_host)) or oauth_ready


def has_usable_sso_authentication():
    if not settings.ENABLE_OIDC_SSO:
        return False
    from plane.license.models import SSOProvider

    return any(
        is_sso_configuration_ready(provider)
        for provider in SSOProvider.objects.filter(is_enabled=True).only(
            "protocol",
            "issuer_url",
            "client_id",
            "client_secret_encrypted",
            "scopes",
            "claim_mappings",
            "configuration_tested_at",
            "configuration_fingerprint",
        )
    )
