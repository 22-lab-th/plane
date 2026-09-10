# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.shortcuts import get_object_or_404
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.license.api.serializers import SSOProviderSerializer
from plane.license.models import Instance, SSOProvider
from plane.authentication.provider.oidc import OIDCConfigurationError, OIDCProviderClient
from plane.authentication.services import record_sso_event
from plane.utils.cache import invalidate_cache

from .base import BaseAPIView


class SSOProviderEndpoint(BaseAPIView):
    def get(self, request):
        providers = SSOProvider.objects.select_related("instance").all()
        return Response(SSOProviderSerializer(providers, many=True).data, status=status.HTTP_200_OK)

    @invalidate_cache(path="/api/instances/", user=False)
    def post(self, request):
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
        provider = self.get_object(pk)
        was_enforced = provider.is_enforced
        serializer = SSOProviderSerializer(provider, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        event = "enforcement_changed" if was_enforced != serializer.instance.is_enforced else "provider_updated"
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
        provider = get_object_or_404(SSOProvider, pk=pk)
        try:
            metadata = OIDCProviderClient(provider).test_configuration()
        except OIDCConfigurationError as exc:
            record_sso_event(
                request,
                "configuration_test",
                "failed",
                provider=provider,
                actor=request.user,
                metadata={"code": exc.code},
            )
            return Response(
                {"status": "failed", "code": exc.code, "error": exc.message},
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider.configuration_tested_at = timezone.now()
        provider.save(update_fields=["configuration_tested_at", "updated_at"])
        record_sso_event(request, "configuration_test", "success", provider=provider, actor=request.user)
        return Response(
            {
                "status": "passed",
                "issuer": metadata["issuer"],
                "configuration_tested_at": provider.configuration_tested_at,
            },
            status=status.HTTP_200_OK,
        )
