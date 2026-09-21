# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A refused legacy avatar delete must not remove the row that names the bytes.

NOTE-T122 leftover 1: ``authentication/adapter/base.py``'s ``delete_old_avatar``
deleted the old avatar object, discarded ``delete_files``' answer and deleted the
``FileAsset`` row regardless. ``DeleteObjects`` reports per-key failures *inside*
an HTTP 200 response, so the row could disappear while its bytes stayed in the
bucket: an orphan with no record left to name the key or to retry from. The
delete's verdict now decides whether the row goes, the way the purge path already
reads it (T-122, F-1).

The provider is faked at the botocore call - where the 200-with-``Errors`` answer
is made - so the adapter's real response handling runs, and both answers stay
deterministic without a reachable provider.
"""

# Python imports
from unittest import mock

# Third party imports
import pytest
from botocore.client import BaseClient

# Module imports
from plane.authentication.adapter.base import Adapter
from plane.db.models import FileAsset

AVATAR_KEY = "user-6c1f0a/old-avatar.png"


def delete_objects_answer(*, refuses):
    """Patch ``DeleteObjects`` to answer with, or without, a per-key error."""
    real_api_call = BaseClient._make_api_call

    def fake_api_call(self, operation_name, api_params):
        if operation_name != "DeleteObjects":
            return real_api_call(self, operation_name, api_params)

        keys = [obj["Key"] for obj in api_params["Delete"]["Objects"]]
        if refuses:
            return {
                "Deleted": [],
                "Errors": [
                    {"Key": key, "Code": "InternalError", "Message": "the key was not deleted"} for key in keys
                ],
            }
        return {"Deleted": [{"Key": key} for key in keys], "Errors": []}

    return mock.patch.object(BaseClient, "_make_api_call", fake_api_call)


def stored_avatar(user):
    """The legacy row and the user pointer a previous login left behind."""
    asset = FileAsset.objects.create(
        attributes={"name": "old-avatar.png", "type": "image/png", "size": 12},
        asset=AVATAR_KEY,
        size=12,
        user=user,
        created_by=user,
        entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
        is_uploaded=True,
    )
    user.avatar_asset = asset
    user.save()
    return asset


@pytest.mark.unit
@pytest.mark.django_db
def test_a_refused_delete_keeps_the_row_and_the_user_pointer(create_user):
    asset = stored_avatar(create_user)

    with delete_objects_answer(refuses=True):
        Adapter(request=None, provider="google").delete_old_avatar(user=create_user)

    # The bytes are still stored, so the row that names them must still exist -
    # and so must the pointer that keeps it reachable - for a later attempt to
    # retry the same key.
    assert FileAsset.objects.filter(pk=asset.pk).exists(), "the row naming the stored key was removed"
    create_user.refresh_from_db()
    assert create_user.avatar_asset_id == asset.pk


@pytest.mark.unit
@pytest.mark.django_db
def test_a_completed_delete_removes_the_row_and_clears_the_user_pointer(create_user):
    asset = stored_avatar(create_user)

    with delete_objects_answer(refuses=False):
        Adapter(request=None, provider="google").delete_old_avatar(user=create_user)

    assert FileAsset.objects.filter(pk=asset.pk).exists() is False
    create_user.refresh_from_db()
    assert create_user.avatar_asset_id is None
    assert create_user.avatar == ""
