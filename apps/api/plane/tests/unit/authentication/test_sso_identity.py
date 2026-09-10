# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from plane.authentication.services import SSOIdentityError, SSOIdentityResolver
from plane.db.models import User
from plane.license.models import SSOIdentity


@pytest.mark.django_db
def test_prelinked_subject_wins_when_email_changes(provider):
    user = User.objects.create(email="original@example.com", username="original")
    SSOIdentity.objects.create(provider=provider, user=user, subject="stable-subject")

    resolved_user, is_signup = SSOIdentityResolver(provider).resolve(
        {"sub": "stable-subject", "email": "changed@example.com", "email_verified": True}
    )

    assert resolved_user == user
    assert resolved_user.email == "original@example.com"
    assert is_signup is False


@pytest.mark.django_db
def test_verified_email_auto_link_requires_explicit_policy(provider):
    user = User.objects.create(email="member@example.com", username="member")

    with pytest.raises(SSOIdentityError) as error:
        SSOIdentityResolver(provider).resolve(
            {"sub": "new-subject", "email": "member@example.com", "email_verified": True}
        )

    assert error.value.code == "link_not_allowed"
    assert not SSOIdentity.objects.filter(provider=provider).exists()

    provider.allow_verified_email_auto_link = True
    provider.save()
    resolved_user, is_signup = SSOIdentityResolver(provider).resolve(
        {"sub": "new-subject", "email": "member@example.com", "email_verified": True}
    )
    assert resolved_user == user
    assert is_signup is False


@pytest.mark.django_db
def test_jit_provisioning_applies_domain_and_group_policy(provider):
    provider.jit_provisioning_enabled = True
    provider.allowed_email_domains = ["example.com"]
    provider.allowed_groups = ["plane-users"]
    provider.save()

    user, is_signup = SSOIdentityResolver(provider).resolve(
        {
            "sub": "new-subject",
            "email": "new.member@example.com",
            "email_verified": True,
            "groups": ["Plane-Users"],
            "given_name": "New",
            "family_name": "Member",
        }
    )

    assert is_signup is True
    assert user.email == "new.member@example.com"
    assert user.has_usable_password() is False
    assert SSOIdentity.objects.filter(provider=provider, user=user, subject="new-subject").exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("claims", "expected_code"),
    [
        ({"sub": "one", "email": "member@example.com"}, "unverified_email"),
        ({"sub": "two", "email": "member@denied.example", "email_verified": True}, "domain_denied"),
        (
            {
                "sub": "three",
                "email": "member@example.com",
                "email_verified": True,
                "groups": ["other"],
            },
            "group_denied",
        ),
    ],
)
def test_jit_policy_failure_creates_no_partial_user(provider, claims, expected_code):
    provider.jit_provisioning_enabled = True
    provider.allowed_email_domains = ["example.com"]
    provider.allowed_groups = ["plane-users"]
    provider.save()

    with pytest.raises(SSOIdentityError) as error:
        SSOIdentityResolver(provider).resolve(claims)

    assert error.value.code == expected_code
    assert not User.objects.filter(email=claims.get("email")).exists()
    assert not SSOIdentity.objects.filter(provider=provider, subject=claims["sub"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(("user_state", "expected_code"), [("inactive", "inactive_user"), ("bot", "bot_user")])
def test_prelinked_identity_rejects_noninteractive_accounts(provider, user_state, expected_code):
    user = User.objects.create(email=f"{user_state}@example.com", username=user_state)
    if user_state == "inactive":
        user.is_active = False
    else:
        user.is_bot = True
    user.save()
    SSOIdentity.objects.create(provider=provider, user=user, subject=f"{user_state}-subject")

    with pytest.raises(SSOIdentityError) as error:
        SSOIdentityResolver(provider).resolve({"sub": f"{user_state}-subject"})

    assert error.value.code == expected_code


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("claims", "expected_code"),
    [
        ({"sub": "invalid-email", "email": "not-an-email", "email_verified": True}, "invalid_email"),
        ({"sub": "jit-off", "email": "member@example.com", "email_verified": True}, "jit_disabled"),
    ],
)
def test_first_login_rejects_invalid_email_or_disabled_jit(provider, claims, expected_code):
    with pytest.raises(SSOIdentityError) as error:
        SSOIdentityResolver(provider).resolve(claims)

    assert error.value.code == expected_code


@pytest.mark.django_db
def test_identity_resolver_supports_custom_claim_mapping_and_string_group(provider):
    provider.claim_mappings = {"email": "mail", "email_verified": "mail_verified", "groups": "roles"}
    provider.allowed_groups = ["plane-users"]
    provider.jit_provisioning_enabled = True
    provider.save()

    user, is_signup = SSOIdentityResolver(provider).resolve(
        {
            "sub": "custom-claims",
            "mail": "mapped@example.com",
            "mail_verified": True,
            "roles": "Plane-Users",
        }
    )

    assert is_signup is True
    assert user.email == "mapped@example.com"
