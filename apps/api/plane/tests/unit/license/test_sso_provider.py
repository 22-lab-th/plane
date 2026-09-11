# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from plane.db.models import User
from plane.license.api.serializers import SSOProviderSerializer
from plane.license.models import (
    Instance,
    InstanceAdmin,
    InstanceConfiguration,
    SSOAuditEvent,
    SSOIdentity,
    SSOProvider,
)
from plane.authentication.services import (
    get_sso_configuration_fingerprint,
    get_sso_provider_mode,
    has_viable_break_glass_admin,
)


def get_admin_session(api_client, create=False):
    session_store = import_module(settings.SESSION_ENGINE).SessionStore
    if create:
        return session_store()
    session_key = api_client.cookies[settings.ADMIN_SESSION_COOKIE_NAME].value
    return session_store(session_key)


@pytest.fixture
def instance(db):
    return Instance.objects.create(
        instance_name="Test Plane",
        instance_id="test-plane",
        current_version="1.0.0",
        last_checked_at=timezone.now(),
    )


@pytest.fixture
def provider(instance):
    sso_provider = SSOProvider(
        instance=instance,
        name="Example Identity",
        slug="example",
        issuer_url="https://id.example.com",
        client_id="plane",
        scopes=["openid", "email", "profile"],
    )
    sso_provider.set_client_secret("top-secret")
    sso_provider.save()
    return sso_provider


@pytest.mark.django_db
def test_provider_serializer_encrypts_and_masks_secret(instance):
    serializer = SSOProviderSerializer(
        data={
            "name": "Example Identity",
            "slug": "example",
            "issuer_url": "https://id.example.com",
            "client_id": "plane",
            "client_secret": "top-secret",
            "scopes": ["openid", "email"],
        }
    )
    serializer.is_valid(raise_exception=True)
    provider = serializer.save(instance=instance)

    assert provider.client_secret_encrypted != "top-secret"
    assert provider.get_client_secret() == "top-secret"
    assert serializer.data["client_secret_configured"] is True
    assert "client_secret" not in serializer.data
    assert "client_secret_encrypted" not in serializer.data


@pytest.mark.django_db
def test_provider_secret_is_preserved_when_unset_on_patch(provider):
    encrypted_secret = provider.client_secret_encrypted
    serializer = SSOProviderSerializer(provider, data={"name": "Renamed"}, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()

    provider.refresh_from_db()
    assert provider.client_secret_encrypted == encrypted_secret
    assert provider.get_client_secret() == "top-secret"


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
def test_lifecycle_transitions_are_reversible_and_preserve_provider_data(provider, create_user):
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    identity = SSOIdentity.objects.create(provider=provider, user=create_user, subject="stable-subject")
    encrypted_secret = provider.client_secret_encrypted

    enable = SSOProviderSerializer(provider, data={"is_enabled": True}, partial=True)
    enable.is_valid(raise_exception=True)
    provider = enable.save()

    assert get_sso_provider_mode(provider).value == "optional"

    disable = SSOProviderSerializer(provider, data={"is_enabled": False}, partial=True)
    disable.is_valid(raise_exception=True)
    provider = disable.save()

    assert get_sso_provider_mode(provider).value == "disabled"
    assert provider.client_secret_encrypted == encrypted_secret
    assert SSOIdentity.objects.filter(pk=identity.pk, provider=provider).exists()


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
def test_lifecycle_always_allows_enforced_provider_to_be_unenforced(provider):
    provider.is_enabled = True
    provider.is_enforced = True
    provider.configuration_tested_at = None
    provider.configuration_fingerprint = ""
    provider.save()

    serializer = SSOProviderSerializer(provider, data={"is_enforced": False}, partial=True)
    serializer.is_valid(raise_exception=True)
    provider = serializer.save()

    assert get_sso_provider_mode(provider).value == "optional"


@pytest.mark.django_db
def test_claim_mapping_values_must_be_non_empty_strings(instance):
    serializer = SSOProviderSerializer(
        data={
            "name": "Example Identity",
            "slug": "example",
            "issuer_url": "https://id.example.com",
            "client_id": "plane",
            "client_secret": "top-secret",
            "scopes": ["openid", "email"],
            "claim_mappings": {"email": ["mail"]},
        }
    )

    assert serializer.is_valid() is False
    assert "claim_mappings" in serializer.errors


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=False)
def test_provider_cannot_be_enabled_when_deployment_gate_is_off(provider):
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()
    serializer = SSOProviderSerializer(provider, data={"is_enabled": True}, partial=True)

    with pytest.raises(ValidationError):
        serializer.is_valid(raise_exception=True)


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=False)
def test_public_instance_config_hides_stored_sso_when_gate_is_off(api_client, instance, provider):
    provider.is_enabled = True
    provider.is_enforced = True
    provider.configuration_tested_at = timezone.now()
    provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
    provider.save()

    response = api_client.get("/api/instances/")

    assert response.status_code == 200
    assert response.data["config"]["is_oidc_sso_available"] is False
    assert response.data["config"]["sso_providers"] == []
    assert response.data["config"]["is_sso_enforced"] is False


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=False, SKIP_ENV_VAR=True)
def test_configuration_update_cannot_disable_final_authentication_method(api_client, create_user, instance):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)
    for key, value in {
        "ENABLE_EMAIL_PASSWORD": "1",
        "ENABLE_MAGIC_LINK_LOGIN": "0",
        "EMAIL_HOST": "",
        "IS_GOOGLE_ENABLED": "0",
        "IS_GITHUB_ENABLED": "0",
        "IS_GITLAB_ENABLED": "0",
        "IS_GITEA_ENABLED": "0",
    }.items():
        InstanceConfiguration.objects.create(key=key, value=value, category="AUTHENTICATION")

    response = api_client.patch("/api/instances/configurations/", {"ENABLE_EMAIL_PASSWORD": "0"}, format="json")

    assert response.status_code == 409
    assert InstanceConfiguration.objects.get(key="ENABLE_EMAIL_PASSWORD").value == "1"
    assert SSOAuditEvent.objects.filter(
        event="authentication_configuration", outcome="denied", actor=create_user
    ).exists()


@pytest.mark.django_db
@override_settings(SSO_BREAK_GLASS_ADMIN_EMAILS={"recovery@example.com"}, SSO_RECOVERY_TEST_MAX_AGE_SECONDS=86400)
def test_break_glass_admin_requires_verified_password_and_recent_recovery_test(instance, provider):
    user = User.objects.create(email="recovery@example.com", username="recovery", is_email_verified=True)
    user.set_password("correct-password")
    user.save()
    InstanceAdmin.objects.create(user=user, instance=instance, role=20, is_verified=True)

    assert has_viable_break_glass_admin(provider) is False

    provider.recovery_tested_at = timezone.now()
    provider.recovery_tested_by = user
    provider.save()
    assert has_viable_break_glass_admin(provider) is True


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
@patch("plane.license.api.views.sso.OIDCProviderClient.get_discovery_document")
def test_admin_interactive_test_uses_distinct_callback_and_pkce(
    mock_discovery, api_client, create_user, instance, provider
):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)
    mock_discovery.return_value = {"authorization_endpoint": "https://id.example.com/authorize"}

    response = api_client.get(f"/api/instances/sso/providers/{provider.id}/test-login/")

    assert response.status_code == 302
    query = parse_qs(urlparse(response.url).query)
    assert query["redirect_uri"] == ["http://testserver/api/instances/sso/providers/test-callback/"]
    assert query["code_challenge_method"] == ["S256"]
    assert get_admin_session(api_client)["oidc_admin_test_transaction"]["provider_id"] == str(provider.id)


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
@patch("plane.license.api.views.sso.OIDCProviderClient.validate_id_token")
@patch("plane.license.api.views.sso.OIDCProviderClient.exchange_code")
def test_admin_interactive_callback_marks_current_configuration_ready(
    mock_exchange, mock_validate, api_client, create_user, instance, provider
):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)
    mock_exchange.return_value = ({"id_token": "signed-token"}, {"issuer": provider.issuer_url})
    mock_validate.return_value = {"sub": "admin-test-subject"}
    session = get_admin_session(api_client, create=True)
    session["oidc_admin_test_transaction"] = {
        "provider_id": str(provider.id),
        "state": "expected-state",
        "nonce": "expected-nonce",
        "code_verifier": "verifier",
        "created_at": int(timezone.now().timestamp()),
        "correlation_id": "interactive-test-correlation",
    }
    session.save()
    api_client.cookies[settings.ADMIN_SESSION_COOKIE_NAME] = session.session_key

    response = api_client.get("/api/instances/sso/providers/test-callback/?code=valid-code&state=expected-state")

    assert response.status_code == 302
    assert "oidc_test=success" in response.url
    provider.refresh_from_db()
    assert provider.configuration_tested_at is not None
    assert provider.configuration_fingerprint == get_sso_configuration_fingerprint(provider)
    assert not SSOIdentity.objects.filter(provider=provider).exists()
    assert SSOAuditEvent.objects.filter(
        provider=provider,
        event="interactive_test",
        outcome="success",
        correlation_id="interactive-test-correlation",
    ).exists()


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
@patch("plane.license.api.views.sso.OIDCProviderClient.exchange_code")
def test_admin_interactive_callback_rejects_invalid_state(mock_exchange, api_client, create_user, instance, provider):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)
    session = get_admin_session(api_client, create=True)
    session["oidc_admin_test_transaction"] = {
        "provider_id": str(provider.id),
        "state": "expected-state",
        "nonce": "expected-nonce",
        "code_verifier": "verifier",
        "created_at": int(timezone.now().timestamp()),
    }
    session.save()
    api_client.cookies[settings.ADMIN_SESSION_COOKIE_NAME] = session.session_key

    response = api_client.get("/api/instances/sso/providers/test-callback/?code=valid-code&state=wrong-state")

    assert response.status_code == 302
    assert "oidc_test=failed" in response.url
    mock_exchange.assert_not_called()
    provider.refresh_from_db()
    assert provider.configuration_tested_at is None


@pytest.mark.django_db
@override_settings(
    ENABLE_OIDC_SSO=True,
    SSO_BREAK_GLASS_ADMIN_EMAILS={"recovery@example.com"},
    SSO_RECOVERY_TEST_MAX_AGE_SECONDS=86400,
)
def test_recovery_endpoint_requires_current_admin_password(api_client, instance, provider):
    user = User.objects.create(email="recovery@example.com", username="recovery", is_email_verified=True)
    user.set_password("correct-password")
    user.save()
    InstanceAdmin.objects.create(user=user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=user)

    denied = api_client.post(
        f"/api/instances/sso/providers/{provider.id}/test-recovery/", {"password": "wrong"}, format="json"
    )
    passed = api_client.post(
        f"/api/instances/sso/providers/{provider.id}/test-recovery/",
        {"password": "correct-password"},
        format="json",
    )

    assert denied.status_code == 400
    assert passed.status_code == 200
    provider.refresh_from_db()
    assert provider.recovery_tested_by == user


@pytest.mark.django_db
def test_provider_subject_is_unique(provider):
    first_user = User.objects.create(email="first@example.com", username="first")
    second_user = User.objects.create(email="second@example.com", username="second")
    SSOIdentity.objects.create(provider=provider, user=first_user, subject="stable-subject")

    with pytest.raises(IntegrityError), transaction.atomic():
        SSOIdentity.objects.create(provider=provider, user=second_user, subject="stable-subject")


@pytest.mark.django_db
def test_only_one_provider_can_be_enabled_per_instance(instance, provider):
    provider.is_enabled = True
    provider.save()
    second_provider = SSOProvider(
        instance=instance,
        name="Second Identity",
        slug="second",
        issuer_url="https://second-id.example.com",
        client_id="plane-second",
        scopes=["openid", "email"],
        is_enabled=True,
    )
    second_provider.set_client_secret("top-secret")

    with pytest.raises(IntegrityError), transaction.atomic():
        second_provider.save()


@pytest.mark.django_db
def test_anonymous_user_cannot_manage_sso_providers(api_client):
    response = api_client.get("/api/instances/sso/providers/")

    assert response.status_code in {401, 403}


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
def test_instance_admin_can_create_provider(api_client, create_user, instance):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)

    response = api_client.post(
        "/api/instances/sso/providers/",
        {
            "name": "Example Identity",
            "slug": "example",
            "issuer_url": "https://id.example.com",
            "client_id": "plane",
            "client_secret": "top-secret",
            "scopes": ["openid", "email"],
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["client_secret_configured"] is True
    assert "client_secret" not in response.data
    assert "client_secret_encrypted" not in response.data


@pytest.mark.django_db
@override_settings(ENABLE_OIDC_SSO=True)
def test_enabled_or_linked_provider_cannot_be_deleted(api_client, create_user, instance, provider):
    InstanceAdmin.objects.create(user=create_user, instance=instance, role=20, is_verified=True)
    api_client.force_authenticate(user=create_user)

    provider.is_enabled = True
    provider.save()
    enabled_response = api_client.delete(f"/api/instances/sso/providers/{provider.id}/")
    assert enabled_response.status_code == 409

    provider.is_enabled = False
    provider.save()
    SSOIdentity.objects.create(provider=provider, user=create_user, subject="admin-subject")
    linked_response = api_client.delete(f"/api/instances/sso/providers/{provider.id}/")
    assert linked_response.status_code == 409
