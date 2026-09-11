import pytest
from django.test import override_settings

from plane.authentication.services import has_normal_authentication_method


AUTH_ENVIRONMENT_KEYS = [
    "ENABLE_EMAIL_PASSWORD",
    "ENABLE_MAGIC_LINK_LOGIN",
    "EMAIL_HOST",
    "IS_GOOGLE_ENABLED",
    "IS_GITHUB_ENABLED",
    "IS_GITLAB_ENABLED",
    "IS_GITEA_ENABLED",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITLAB_CLIENT_ID",
    "GITLAB_CLIENT_SECRET",
    "GITLAB_HOST",
    "GITEA_CLIENT_ID",
    "GITEA_CLIENT_SECRET",
    "GITEA_HOST",
]


@pytest.fixture
def disabled_auth_environment(monkeypatch):
    for key in AUTH_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "0" if key.startswith(("ENABLE_", "IS_")) else "")


@override_settings(SKIP_ENV_VAR=False)
def test_password_auth_is_a_usable_normal_method(disabled_auth_environment, monkeypatch):
    monkeypatch.setenv("ENABLE_EMAIL_PASSWORD", "1")

    assert has_normal_authentication_method() is True


@override_settings(SKIP_ENV_VAR=False)
def test_magic_link_requires_smtp(disabled_auth_environment, monkeypatch):
    monkeypatch.setenv("ENABLE_MAGIC_LINK_LOGIN", "1")
    assert has_normal_authentication_method() is False

    monkeypatch.setenv("EMAIL_HOST", "smtp.example.com")
    assert has_normal_authentication_method() is True


@override_settings(SKIP_ENV_VAR=False)
def test_enabled_oauth_requires_client_credentials(disabled_auth_environment, monkeypatch):
    monkeypatch.setenv("IS_GOOGLE_ENABLED", "1")
    assert has_normal_authentication_method() is False

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "plane")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    assert has_normal_authentication_method() is True
