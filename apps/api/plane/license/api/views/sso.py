# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

# Django imports
from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.license.api.serializers import SSOProviderSerializer
from plane.license.models import Instance, InstanceAdmin, SSOProvider
from plane.authentication.provider.oidc import OIDCConfigurationError, OIDCProviderClient
from plane.authentication.rate_limit import SSORecoveryTestThrottle
from plane.authentication.services import (
    get_sso_configuration_fingerprint,
    get_sso_correlation_id,
    is_break_glass_email,
    record_sso_event,
)
from plane.authentication.utils.host import base_host
from plane.utils.cache import invalidate_cache

from .base import BaseAPIView

OIDC_ADMIN_TEST_TRANSACTION_MAX_AGE_SECONDS = 10 * 60


def _interactive_test_callback_uri(request):
    return request.build_absolute_uri("/api/instances/sso/providers/test-callback/")


def _interactive_test_redirect(request, outcome, code=None):
    params = {"oidc_test": outcome}
    if code:
        params["code"] = code
    return HttpResponseRedirect(f"{base_host(request=request, is_admin=True)}authentication/sso?{urlencode(params)}")


class SSOProviderEndpoint(BaseAPIView):
    def get(self, request):
        providers = SSOProvider.objects.select_related("instance").all()
        return Response(SSOProviderSerializer(providers, many=True).data, status=status.HTTP_200_OK)

    @invalidate_cache(path="/api/instances/", user=False)
    def post(self, request):
        if not settings.ENABLE_OIDC_SSO:
            return Response({"error": "OIDC SSO is disabled for this deployment."}, status=status.HTTP_409_CONFLICT)
        instance = Instance.objects.first()
        if instance is None:
            return Response({"error": "Instance is not configured"}, status=status.HTTP_400_BAD_REQUEST)
        serializer = SSOProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(instance=instance)
        record_sso_event(request, "provider_created", "success", provider=serializer.instance, actor=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SSOProviderDetailEndpoint(BaseAPIView):
    def get_object(self, pk):
        return get_object_or_404(SSOProvider, pk=pk)

    def get(self, request, pk):
        return Response(SSOProviderSerializer(self.get_object(pk)).data, status=status.HTTP_200_OK)

    @invalidate_cache(path="/api/instances/", user=False)
    def patch(self, request, pk):
        if not settings.ENABLE_OIDC_SSO:
            return Response({"error": "OIDC SSO is disabled for this deployment."}, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            provider = SSOProvider.objects.select_for_update().get(pk=pk)
            was_enabled = provider.is_enabled
            was_enforced = provider.is_enforced
            serializer = SSOProviderSerializer(provider, data=request.data, partial=True)
            if not serializer.is_valid():
                record_sso_event(
                    request,
                    "provider_transition",
                    "denied",
                    provider=provider,
                    actor=request.user,
                    metadata={"fields": ",".join(sorted(serializer.errors))},
                )
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            serializer.save()
        events = []
        if was_enforced != serializer.instance.is_enforced:
            events.append("provider_enforced" if serializer.instance.is_enforced else "provider_unenforced")
        if was_enabled != serializer.instance.is_enabled:
            events.append("provider_enabled" if serializer.instance.is_enabled else "provider_disabled")
        for event in events or ["provider_updated"]:
            record_sso_event(
                request,
                event,
                "success",
                provider=serializer.instance,
                actor=request.user,
                metadata={"is_enforced": serializer.instance.is_enforced},
            )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @invalidate_cache(path="/api/instances/", user=False)
    def delete(self, request, pk):
        if not settings.ENABLE_OIDC_SSO:
            return Response({"error": "OIDC SSO is disabled for this deployment."}, status=status.HTTP_409_CONFLICT)
        provider = self.get_object(pk)
        if provider.is_enabled or provider.is_enforced:
            record_sso_event(request, "provider_delete", "denied", provider=provider, actor=request.user)
            return Response(
                {"error": "Disable and unenforce the SSO provider before deleting it."},
                status=status.HTTP_409_CONFLICT,
            )
        if provider.identities.exists():
            record_sso_event(request, "provider_delete", "denied", provider=provider, actor=request.user)
            return Response(
                {"error": "An SSO provider with linked identities cannot be deleted."},
                status=status.HTTP_409_CONFLICT,
            )
        record_sso_event(request, "provider_deleted", "success", provider=provider, actor=request.user)
        provider.delete(soft=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SSOProviderTestEndpoint(BaseAPIView):
    @invalidate_cache(path="/api/instances/", user=False)
    def post(self, request, pk):
        if not settings.ENABLE_OIDC_SSO:
            return Response(
                {"status": "failed", "code": "oidc_disabled", "error": "OIDC SSO is disabled for this deployment."},
                status=status.HTTP_409_CONFLICT,
            )
        provider = get_object_or_404(SSOProvider, pk=pk)
        try:
            metadata = OIDCProviderClient(provider).test_configuration()
        except OIDCConfigurationError as exc:
            record_sso_event(
                request,
                "metadata_test",
                "failed",
                provider=provider,
                actor=request.user,
                metadata={"code": exc.code},
            )
            return Response(
                {"status": "failed", "code": exc.code, "error": exc.message},
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider.metadata_tested_at = timezone.now()
        provider.save(update_fields=["metadata_tested_at", "updated_at"])
        record_sso_event(request, "metadata_test", "success", provider=provider, actor=request.user)
        return Response(
            {
                "status": "passed",
                "issuer": metadata["issuer"],
                "metadata_tested_at": provider.metadata_tested_at,
            },
            status=status.HTTP_200_OK,
        )


class SSOProviderInteractiveTestInitiateEndpoint(BaseAPIView):
    def get(self, request, pk):
        if not settings.ENABLE_OIDC_SSO:
            return _interactive_test_redirect(request, "failed", "oidc_disabled")
        provider = get_object_or_404(SSOProvider, pk=pk)
        if not provider.client_secret_configured:
            return _interactive_test_redirect(request, "failed", "missing_client_secret")
        try:
            metadata = OIDCProviderClient(provider).get_discovery_document(refresh=True)
        except OIDCConfigurationError as exc:
            record_sso_event(
                request,
                "interactive_test",
                "failed",
                provider=provider,
                actor=request.user,
                metadata={"code": exc.code},
            )
            return _interactive_test_redirect(request, "failed", exc.code)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        request.session["oidc_admin_test_transaction"] = {
            "provider_id": str(provider.id),
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "created_at": int(time.time()),
            "correlation_id": get_sso_correlation_id(request),
        }
        request.session.save()
        params = {
            "client_id": provider.client_id,
            "redirect_uri": _interactive_test_callback_uri(request),
            "response_type": "code",
            "scope": " ".join(provider.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        record_sso_event(request, "interactive_test_started", "success", provider=provider, actor=request.user)
        return HttpResponseRedirect(f"{metadata['authorization_endpoint']}?{urlencode(params)}")


class SSOProviderInteractiveTestCallbackEndpoint(BaseAPIView):
    def get(self, request):
        transaction_data = request.session.pop("oidc_admin_test_transaction", None)
        request.session.modified = True
        if not settings.ENABLE_OIDC_SSO or not isinstance(transaction_data, dict):
            record_sso_event(
                request, "interactive_test", "failed", actor=request.user, metadata={"code": "invalid_transaction"}
            )
            return _interactive_test_redirect(request, "failed", "invalid_transaction")
        request.sso_correlation_id = transaction_data.get("correlation_id") or get_sso_correlation_id(request)

        received_state = request.GET.get("state", "")
        expected_state = transaction_data.get("state", "")
        created_at = transaction_data.get("created_at", 0)
        if (
            not received_state
            or not expected_state
            or not secrets.compare_digest(received_state, expected_state)
            or not isinstance(created_at, int)
            or time.time() - created_at > OIDC_ADMIN_TEST_TRANSACTION_MAX_AGE_SECONDS
            or time.time() < created_at
        ):
            record_sso_event(
                request,
                "interactive_test",
                "failed",
                actor=request.user,
                metadata={"code": "invalid_transaction"},
            )
            return _interactive_test_redirect(request, "failed", "invalid_transaction")
        provider = SSOProvider.objects.filter(pk=transaction_data.get("provider_id")).first()
        code = request.GET.get("code")
        if provider is None or not code:
            return _interactive_test_redirect(request, "failed", "missing_code")
        try:
            client = OIDCProviderClient(provider)
            token_data, metadata = client.exchange_code(
                code=code,
                code_verifier=transaction_data["code_verifier"],
                redirect_uri=_interactive_test_callback_uri(request),
            )
            client.validate_id_token(token_data, metadata, transaction_data["nonce"])
        except (OIDCConfigurationError, KeyError) as exc:
            code_name = exc.code if isinstance(exc, OIDCConfigurationError) else "missing_value"
            record_sso_event(
                request,
                "interactive_test",
                "failed",
                provider=provider,
                actor=request.user,
                metadata={"code": code_name},
            )
            return _interactive_test_redirect(request, "failed", code_name)

        provider.configuration_tested_at = timezone.now()
        provider.configuration_fingerprint = get_sso_configuration_fingerprint(provider)
        provider.save(update_fields=["configuration_tested_at", "configuration_fingerprint", "updated_at"])
        record_sso_event(request, "interactive_test", "success", provider=provider, actor=request.user)
        return _interactive_test_redirect(request, "success")


class SSOProviderRecoveryTestEndpoint(BaseAPIView):
    throttle_classes = [SSORecoveryTestThrottle]

    @invalidate_cache(path="/api/instances/", user=False)
    def post(self, request, pk):
        if not settings.ENABLE_OIDC_SSO:
            return Response(
                {"status": "failed", "code": "oidc_disabled", "error": "OIDC SSO is disabled for this deployment."},
                status=status.HTTP_409_CONFLICT,
            )
        provider = get_object_or_404(SSOProvider, pk=pk)
        password = request.data.get("password")
        admin = InstanceAdmin.objects.filter(instance=provider.instance, user=request.user, is_verified=True).first()
        is_viable = bool(
            admin
            and is_break_glass_email(request.user.email)
            and request.user.is_active
            and not request.user.is_bot
            and request.user.is_email_verified
            and request.user.has_usable_password()
            and isinstance(password, str)
            and request.user.check_password(password)
        )
        if not is_viable:
            record_sso_event(request, "recovery_test", "failed", provider=provider, actor=request.user)
            return Response(
                {"status": "failed", "error": "Recovery credentials could not be verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider.recovery_tested_at = timezone.now()
        provider.recovery_tested_by = request.user
        provider.save(update_fields=["recovery_tested_at", "recovery_tested_by", "updated_at"])
        record_sso_event(request, "recovery_test", "success", provider=provider, actor=request.user)
        return Response(
            {"status": "passed", "recovery_tested_at": provider.recovery_tested_at}, status=status.HTTP_200_OK
        )
