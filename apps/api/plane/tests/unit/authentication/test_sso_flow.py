import time
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from plane.authentication.provider.oidc import OIDCConfigurationError
from plane.authentication.services import get_sso_configuration_fingerprint
from plane.db.models import User
from plane.license.models import InstanceAdmin, SSOAuditEvent, SSOIdentity


@pytest.fixture(autouse=True)
def allow_authentication_requests():
    with (
        patch("plane.authentication.views.app.sso.authentication_throttle_allows", return_value=True),
        patch("plane.license.api.views.admin.authentication_throttle_allows", return_value=True),
    ):
        yield


@pytest.mark.django_db
@patch("plane.authentication.views.app.sso.OIDCProviderClient.get_discovery_document")
def test_sso_initiation_stores_transaction_and_sends_pkce(mock_discovery, api_client, instance, provider):
    instance.is_setup_done = True
    instance.save()
    provider.is_enabled = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    mock_discovery.return_value = {"authorization_endpoint": "https://id.example.com/authorize"}

    response = api_client.get("/auth/sso/example/?next_path=/my-work")

    assert response.status_code == 302
    query = parse_qs(urlparse(response.url).query)
    assert query["client_id"] == ["plane"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"][0]
    assert query["state"][0]
    transaction_data = api_client.session["oidc_transaction"]
    assert transaction_data["state"] == query["state"][0]
    assert transaction_data["code_verifier"]
    assert transaction_data["next_path"] == "/my-work"


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=False)
@patch("plane.authentication.views.app.sso.OIDCProviderClient.get_discovery_document")
def test_deployment_gate_disables_sso_without_contacting_provider(mock_discovery, api_client, instance, provider):
    instance.is_setup_done = True
    instance.save()
    provider.is_enabled = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()

    response = api_client.get("/auth/sso/example/")

    assert response.status_code == 302
    assert "error_code=5126" in response.url
    mock_discovery.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/auth/sso/example/", "/auth/sso/callback/"])
@patch("plane.authentication.views.app.sso.authentication_throttle_allows", return_value=False)
def test_sso_endpoints_are_rate_limited(mock_throttle, api_client, path):
    response = api_client.get(path)

    assert response.status_code == 302
    assert "error_code=5900" in response.url


@pytest.mark.django_db
@patch("plane.authentication.views.app.sso.OIDCProviderClient.get_discovery_document")
def test_sso_initiation_audits_discovery_failure(mock_discovery, api_client, instance, provider):
    instance.is_setup_done = True
    instance.save()
    provider.is_enabled = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    mock_discovery.side_effect = OIDCConfigurationError("issuer_mismatch", "Issuer mismatch")

    response = api_client.get("/auth/sso/example/")

    assert response.status_code == 302
    assert SSOAuditEvent.objects.filter(
        event="login_initiation",
        outcome="failed",
        provider=provider,
        metadata={"category": "discovery", "code": "issuer_mismatch"},
    ).exists()
    audit_event = SSOAuditEvent.objects.get(event="login_initiation", outcome="failed", provider=provider)
    assert audit_event.correlation_id


@pytest.mark.django_db
@patch("plane.authentication.views.app.sso.post_user_auth_workflow")
@patch("plane.authentication.views.app.sso.OIDCProviderClient.validate_id_token")
@patch("plane.authentication.views.app.sso.OIDCProviderClient.exchange_code")
def test_sso_callback_logs_in_prelinked_user_and_rejects_replay(
    mock_exchange, mock_validate, mock_post_auth, api_client, provider
):
    provider.is_enabled = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    user = User.objects.create(email="member@example.com", username="member")
    SSOIdentity.objects.create(provider=provider, user=user, subject="stable-subject")
    mock_exchange.return_value = ({"id_token": "signed-token"}, {"issuer": provider.issuer_url})
    mock_validate.return_value = {"sub": "stable-subject", "email": "changed@example.com"}
    session = api_client.session
    session["oidc_transaction"] = {
        "provider_id": str(provider.id),
        "state": "expected-state",
        "nonce": "expected-nonce",
        "code_verifier": "verifier",
        "created_at": int(time.time()),
        "next_path": "/my-work",
    }
    session.save()

    response = api_client.get("/auth/sso/callback/?code=valid-code&state=expected-state")

    assert response.status_code == 302
    assert "next_path=/my-work" in response.url
    assert api_client.session.get("_auth_user_id") == str(user.id)
    assert "oidc_transaction" not in api_client.session
    mock_post_auth.assert_called_once()

    replay = api_client.get("/auth/sso/callback/?code=valid-code&state=expected-state")
    assert replay.status_code == 302
    assert "error_code=5126" in replay.url
    assert mock_exchange.call_count == 1
    assert SSOAuditEvent.objects.filter(
        event="login", outcome="failed", metadata={"reason": "missing_transaction"}
    ).exists()


@pytest.mark.django_db
@patch("plane.authentication.views.app.sso.OIDCProviderClient.exchange_code")
def test_sso_callback_audits_categorized_oidc_failure(mock_exchange, api_client, provider):
    provider.is_enabled = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    mock_exchange.side_effect = OIDCConfigurationError("upstream_error", "Token exchange failed")
    session = api_client.session
    session["oidc_transaction"] = {
        "provider_id": str(provider.id),
        "state": "expected-state",
        "nonce": "expected-nonce",
        "code_verifier": "verifier",
        "created_at": int(time.time()),
        "next_path": "",
    }
    session.save()

    response = api_client.get("/auth/sso/callback/?code=invalid-code&state=expected-state")

    assert response.status_code == 302
    assert SSOAuditEvent.objects.filter(
        event="login",
        outcome="failed",
        provider=provider,
        metadata={"category": "oidc", "code": "upstream_error"},
    ).exists()


@pytest.mark.django_db
def test_enforcement_blocks_legacy_authentication(api_client, provider):
    provider.is_enabled = True
    provider.is_enforced = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()

    response = api_client.get("/auth/google/")

    assert response.status_code == 302
    assert "error_code=5126" in response.url


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=False)
def test_stored_enforcement_does_not_block_normal_auth_when_gate_is_off(api_client, provider):
    provider.is_enabled = True
    provider.is_enforced = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()

    response = api_client.get("/auth/google/")

    assert response.status_code == 302
    assert "error_code=5126" not in response.url


@pytest.mark.django_db
@override_settings(
    SSO_BREAK_GLASS_ADMIN_EMAILS={"recovery@example.com"},
    SSO_BREAK_GLASS_SESSION_AGE=900,
    SSO_RECOVERY_TEST_MAX_AGE_SECONDS=86400,
)
def test_enforced_sso_allows_only_configured_break_glass_admin(api_client, instance, provider):
    instance.is_setup_done = True
    instance.save()
    provider.is_enabled = True
    provider.is_enforced = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    recovery_user = User.objects.create(email="recovery@example.com", username="recovery")
    recovery_user.set_password("correct-password")
    recovery_user.is_email_verified = True
    recovery_user.save()
    InstanceAdmin.objects.create(instance=instance, user=recovery_user, role=20, is_verified=True)
    provider.recovery_tested_at = timezone.now()
    provider.recovery_tested_by = recovery_user
    provider.save()

    denied = api_client.post(
        "/api/instances/admins/sign-in/",
        {"email": "other@example.com", "password": "correct-password"},
    )
    assert denied.status_code == 302
    assert "error_code=5175" in denied.url

    allowed = api_client.post(
        "/api/instances/admins/sign-in/",
        {"email": "recovery@example.com", "password": "correct-password"},
    )
    assert allowed.status_code == 302
    assert allowed.url.endswith("/god-mode/general/")
    assert int(allowed.cookies[settings.ADMIN_SESSION_COOKIE_NAME]["max-age"]) <= 900


@pytest.mark.django_db
@patch("plane.authentication.views.app.signout.OIDCProviderClient.get_discovery_document")
def test_sso_logout_destroys_local_session_and_uses_optional_rp_logout(mock_discovery, api_client, provider):
    user = User.objects.create(email="member@example.com", username="member")
    api_client.force_login(user)
    session = api_client.session
    session["sso_provider_id"] = str(provider.id)
    session.save()
    mock_discovery.return_value = {
        "end_session_endpoint": "https://id.example.com/logout?source=plane",
    }

    response = api_client.post("/auth/sign-out/")

    assert response.status_code == 302
    assert response.url.startswith("https://id.example.com/logout?")
    query = parse_qs(urlparse(response.url).query)
    assert query["client_id"] == [provider.client_id]
    assert query["post_logout_redirect_uri"] == ["http://localhost:3000"]
    assert "_auth_user_id" not in api_client.session


@pytest.mark.django_db
@patch("plane.authentication.views.app.signout.OIDCProviderClient.get_discovery_document")
def test_sso_logout_remains_local_when_provider_is_unavailable(mock_discovery, api_client, provider):
    user = User.objects.create(email="member@example.com", username="member")
    api_client.force_login(user)
    session = api_client.session
    session["sso_provider_id"] = str(provider.id)
    session.save()
    mock_discovery.side_effect = OIDCConfigurationError("connection_failed", "Unavailable")

    response = api_client.post("/auth/sign-out/")

    assert response.status_code == 302
    assert response.url == "http://localhost:3000"
    assert "_auth_user_id" not in api_client.session
