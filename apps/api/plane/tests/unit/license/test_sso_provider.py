# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from plane.db.models import User
from plane.license.api.serializers import SSOProviderSerializer
from plane.license.models import Instance, InstanceAdmin, SSOIdentity, SSOProvider


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
def test_provider_subject_is_unique(provider):
    first_user = User.objects.create(email="first@example.com", username="first")
    second_user = User.objects.create(email="second@example.com", username="second")
    SSOIdentity.objects.create(provider=provider, user=first_user, subject="stable-subject")

    with pytest.raises(IntegrityError), transaction.atomic():
        SSOIdentity.objects.create(provider=provider, user=second_user, subject="stable-subject")


@pytest.mark.django_db
def test_anonymous_user_cannot_manage_sso_providers(api_client):
    response = api_client.get("/api/instances/sso/providers/")

    assert response.status_code in {401, 403}


@pytest.mark.django_db
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
