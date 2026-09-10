from datetime import datetime

import pytest
import pytz
from freezegun import freeze_time

from plane.authentication.provider.oauth.github import GitHubOAuthProvider
from plane.authentication.provider.oauth.google import GoogleOAuthProvider


@pytest.mark.unit
@freeze_time("2026-09-10 08:00:00")
@pytest.mark.parametrize("provider_class", [GoogleOAuthProvider, GitHubOAuthProvider])
def test_oauth_expires_in_is_relative_to_token_exchange(provider_class, monkeypatch):
    provider = provider_class.__new__(provider_class)
    provider.code = "authorization-code"
    provider.client_id = "client-id"
    provider.client_secret = "client-secret"
    provider.redirect_uri = "https://plane.example.com/auth/callback/"

    monkeypatch.setattr(
        provider,
        "get_user_token",
        lambda **_kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )

    provider.set_token_data()

    assert provider.token_data["access_token_expired_at"] == datetime(2026, 9, 10, 9, 0, tzinfo=pytz.utc)


@pytest.mark.unit
@pytest.mark.parametrize("provider_class", [GoogleOAuthProvider, GitHubOAuthProvider])
def test_oauth_missing_expires_in_keeps_expiry_empty(provider_class, monkeypatch):
    provider = provider_class.__new__(provider_class)
    provider.code = "authorization-code"
    provider.client_id = "client-id"
    provider.client_secret = "client-secret"
    provider.redirect_uri = "https://plane.example.com/auth/callback/"

    monkeypatch.setattr(provider, "get_user_token", lambda **_kwargs: {"access_token": "access-token"})

    provider.set_token_data()

    assert provider.token_data["access_token_expired_at"] is None
