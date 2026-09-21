# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from django.utils import timezone

from plane.db.models import FileAsset
from plane.license.models import Instance, InstanceAdmin, InstanceConfiguration


@pytest.fixture
def storage_admin(api_client, create_user, settings):
    settings.SKIP_ENV_VAR = True
    instance = Instance.objects.create(
        instance_name="Storage tests", instance_id="storage-tests",
        current_version="1", last_checked_at=timezone.now(),
    )
    InstanceAdmin.objects.create(instance=instance, user=create_user, role=20)
    api_client.force_authenticate(user=create_user)
    for key, value in {
        "STORAGE_PROVIDER": "s3", "AWS_S3_BUCKET_NAME": "uploads",
        "AWS_S3_ENDPOINT_URL": "http://test-minio:9000",
        "AWS_S3_ADDRESSING_STYLE": "auto", "SIGNED_URL_EXPIRATION": "3600",
    }.items():
        InstanceConfiguration.objects.create(key=key, value=value, category="STORAGE")
    return api_client


@pytest.mark.django_db
@pytest.mark.parametrize("updates", [
    {"AWS_S3_BUCKET_NAME": "another-bucket"},
    {"AWS_S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com"},
    {"STORAGE_PROVIDER": "r2"},
])
def test_existing_assets_prevent_storage_identity_change(storage_admin, updates):
    FileAsset.objects.create(asset="legacy/file.pdf")
    before = dict(InstanceConfiguration.objects.values_list("key", "value"))
    response = storage_admin.patch("/api/instances/configurations/", updates, format="json")
    assert response.status_code == 409
    assert dict(InstanceConfiguration.objects.values_list("key", "value")) == before


@pytest.mark.django_db
def test_empty_installation_can_select_r2(storage_admin):
    response = storage_admin.patch("/api/instances/configurations/", {
        "STORAGE_PROVIDER": "r2", "AWS_S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
    }, format="json")
    assert response.status_code == 200
    assert InstanceConfiguration.objects.get(key="STORAGE_PROVIDER").value == "r2"


@pytest.mark.django_db
@pytest.mark.parametrize("updates", [
    {"AWS_S3_ADDRESSING_STYLE": "virutal"}, {"SIGNED_URL_EXPIRATION": "bad"},
    {"SIGNED_URL_EXPIRATION": "604801"}, {"SIGNED_URL_EXPIRATION": "0"},
])
def test_invalid_storage_settings_are_not_saved(storage_admin, updates):
    before = dict(InstanceConfiguration.objects.values_list("key", "value"))
    response = storage_admin.patch("/api/instances/configurations/", updates, format="json")
    assert response.status_code == 400
    assert dict(InstanceConfiguration.objects.values_list("key", "value")) == before


@pytest.mark.django_db
def test_existing_assets_allow_ttl_change(storage_admin):
    FileAsset.objects.create(asset="legacy/file.pdf")
    response = storage_admin.patch("/api/instances/configurations/", {
        "SIGNED_URL_EXPIRATION": "600",
    }, format="json")
    assert response.status_code == 200


@pytest.mark.django_db
def test_partial_credential_pair_is_rejected(storage_admin):
    InstanceConfiguration.objects.create(key="AWS_ACCESS_KEY_ID", value="access", category="STORAGE")
    InstanceConfiguration.objects.create(key="AWS_SECRET_ACCESS_KEY", value="secret", category="STORAGE")
    response = storage_admin.patch("/api/instances/configurations/", {"AWS_SECRET_ACCESS_KEY": ""}, format="json")
    assert response.status_code == 400
    assert InstanceConfiguration.objects.get(key="AWS_SECRET_ACCESS_KEY").value == "secret"


@pytest.mark.django_db
def test_both_credentials_can_be_cleared_for_ambient_chain(storage_admin):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        InstanceConfiguration.objects.create(key=key, value="old", category="STORAGE")
    response = storage_admin.patch("/api/instances/configurations/", {
        "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "",
    }, format="json")
    assert response.status_code == 200
