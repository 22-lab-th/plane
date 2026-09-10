import pytest
from django.utils import timezone

from plane.license.models import Instance, SSOProvider


@pytest.fixture
def instance(db):
    return Instance.objects.create(
        instance_name="Test Plane",
        instance_id="test-plane-authentication",
        current_version="1.0.0",
        last_checked_at=timezone.now(),
    )


@pytest.fixture
def provider(instance):
    sso_provider = SSOProvider.objects.create(
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
