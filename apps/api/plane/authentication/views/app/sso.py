# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.conf import settings
from django.utils import timezone
from django.views import View

from plane.authentication.adapter.error import AUTHENTICATION_ERROR_CODES, AuthenticationException
from plane.authentication.provider.oidc import OIDCConfigurationError, OIDCProviderClient
from plane.authentication.rate_limit import authentication_throttle_allows
from plane.authentication.services import SSOIdentityError, SSOIdentityResolver, is_sso_configuration_ready
from plane.authentication.services import record_sso_event
from plane.authentication.utils.host import base_host
from plane.authentication.utils.login import user_login
from plane.authentication.utils.redirection_path import get_redirection_path
from plane.authentication.utils.user_auth_workflow import post_user_auth_workflow
from plane.license.models import Instance, SSOIdentity, SSOProvider
from plane.utils.ip_address import get_client_ip
from plane.utils.path_validator import get_safe_redirect_url, validate_next_path

OIDC_TRANSACTION_MAX_AGE_SECONDS = 10 * 60


def _callback_uri(request):
    return request.build_absolute_uri("/auth/sso/callback/")


def _error_redirect(request, next_path=None, error_name="SSO_AUTHENTICATION_FAILED"):
    error = AuthenticationException(
        error_code=AUTHENTICATION_ERROR_CODES[error_name],
        error_message=error_name,
    )
    return HttpResponseRedirect(
        get_safe_redirect_url(
            base_url=base_host(request=request, is_app=True),
            next_path=next_path,
            params=error.get_error_dict(),
        )
    )


class SSOInitiateEndpoint(View):
    def get(self, request, provider_slug):
        next_path = validate_next_path(request.GET.get("next_path", ""))
        if not settings.ENABLE_OIDC_SSO:
            return _error_redirect(request, next_path)
        if not authentication_throttle_allows(request):
            return _error_redirect(request, next_path, "RATE_LIMIT_EXCEEDED")
        instance = Instance.objects.first()
        if instance is None or not instance.is_setup_done:
            return _error_redirect(request, next_path)
        provider = SSOProvider.objects.filter(instance=instance, slug=provider_slug, is_enabled=True).first()
        if provider is None or not is_sso_configuration_ready(provider):
            return _error_redirect(request, next_path)

        try:
            metadata = OIDCProviderClient(provider).get_discovery_document()
        except OIDCConfigurationError as exc:
            record_sso_event(
                request,
                "login_initiation",
                "failed",
                provider=provider,
                metadata={"category": "discovery", "code": exc.code},
            )
            return _error_redirect(request, next_path)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        request.session["oidc_transaction"] = {
            "provider_id": str(provider.id),
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "created_at": int(time.time()),
            "next_path": next_path,
        }
        request.session.save()

        params = {
            "client_id": provider.client_id,
            "redirect_uri": _callback_uri(request),
            "response_type": "code",
            "scope": " ".join(provider.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return HttpResponseRedirect(f"{metadata['authorization_endpoint']}?{urlencode(params)}")


class SSOCallbackEndpoint(View):
    def get(self, request):
        if not settings.ENABLE_OIDC_SSO:
            return _error_redirect(request)
        if not authentication_throttle_allows(request):
            return _error_redirect(request, error_name="RATE_LIMIT_EXCEEDED")
        transaction_data = request.session.pop("oidc_transaction", None)
        request.session.modified = True
        next_path = transaction_data.get("next_path") if isinstance(transaction_data, dict) else None
        if not isinstance(transaction_data, dict):
            record_sso_event(request, "login", "failed", metadata={"reason": "missing_transaction"})
            return _error_redirect(request, next_path)

        received_state = request.GET.get("state", "")
        expected_state = transaction_data.get("state", "")
        created_at = transaction_data.get("created_at", 0)
        if (
            not received_state
            or not expected_state
            or not secrets.compare_digest(received_state, expected_state)
            or not isinstance(created_at, int)
            or time.time() - created_at > OIDC_TRANSACTION_MAX_AGE_SECONDS
            or time.time() < created_at
        ):
            record_sso_event(request, "login", "failed", metadata={"reason": "invalid_transaction"})
            return _error_redirect(request, next_path)
        code = request.GET.get("code")
        if not code:
            record_sso_event(request, "login", "failed", metadata={"reason": "missing_code"})
            return _error_redirect(request, next_path)

        provider = SSOProvider.objects.filter(pk=transaction_data.get("provider_id"), is_enabled=True).first()
        if provider is None or not is_sso_configuration_ready(provider):
            record_sso_event(request, "login", "failed", metadata={"reason": "provider_unavailable"})
            return _error_redirect(request, next_path)

        try:
            client = OIDCProviderClient(provider)
            token_data, metadata = client.exchange_code(
                code=code,
                code_verifier=transaction_data["code_verifier"],
                redirect_uri=_callback_uri(request),
            )
            claims = client.validate_id_token(token_data, metadata, transaction_data["nonce"])
            identity_existed = SSOIdentity.objects.filter(provider=provider, subject=claims["sub"]).exists()
            user, is_signup = SSOIdentityResolver(provider).resolve(claims)
        except OIDCConfigurationError as exc:
            record_sso_event(
                request,
                "login",
                "failed",
                provider=provider,
                metadata={"category": "oidc", "code": exc.code},
            )
            return _error_redirect(request, next_path)
        except SSOIdentityError as exc:
            record_sso_event(
                request,
                "login",
                "failed",
                provider=provider,
                metadata={"category": "identity_policy", "code": exc.code},
            )
            return _error_redirect(request, next_path)
        except KeyError:
            record_sso_event(
                request,
                "login",
                "failed",
                provider=provider,
                metadata={"category": "transaction", "code": "missing_value"},
            )
            return _error_redirect(request, next_path)

        now = timezone.now()
        user.last_login_medium = f"sso:{provider.slug}"[:20]
        user.last_active = now
        user.last_login_time = now
        user.last_login_ip = get_client_ip(request=request)
        user.last_login_uagent = request.META.get("HTTP_USER_AGENT", "")
        user.token_updated_at = now
        user.save()
        post_user_auth_workflow(user, is_signup, request)
        user_login(request=request, user=user, is_app=True)
        request.session["sso_provider_id"] = str(provider.id)
        if not identity_existed:
            record_sso_event(
                request,
                "identity_linked",
                "success",
                provider=provider,
                actor=user,
                metadata={"mode": "jit" if is_signup else "verified_email"},
            )
        record_sso_event(request, "login", "success", provider=provider, actor=user)
        path = next_path or get_redirection_path(user=user)
        return HttpResponseRedirect(
            get_safe_redirect_url(base_url=base_host(request=request, is_app=True), next_path=path, params={})
        )
