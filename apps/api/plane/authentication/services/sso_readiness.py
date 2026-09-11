# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import hashlib
import json
import os

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
    ) = get_configuration_value(
        [
            {"key": "ENABLE_EMAIL_PASSWORD", "default": os.environ.get("ENABLE_EMAIL_PASSWORD", "1")},
            {"key": "ENABLE_MAGIC_LINK_LOGIN", "default": os.environ.get("ENABLE_MAGIC_LINK_LOGIN", "1")},
            {"key": "EMAIL_HOST", "default": os.environ.get("EMAIL_HOST", "")},
            {"key": "IS_GOOGLE_ENABLED", "default": os.environ.get("IS_GOOGLE_ENABLED", "0")},
            {"key": "IS_GITHUB_ENABLED", "default": os.environ.get("IS_GITHUB_ENABLED", "0")},
            {"key": "IS_GITLAB_ENABLED", "default": os.environ.get("IS_GITLAB_ENABLED", "0")},
            {"key": "IS_GITEA_ENABLED", "default": os.environ.get("IS_GITEA_ENABLED", "0")},
        ]
    )
    return (
        email_password == "1"
        or (magic_link == "1" and bool(email_host))
        or any(value == "1" for value in (google, github, gitlab, gitea))
    )
