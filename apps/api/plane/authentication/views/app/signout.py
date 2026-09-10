# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.views import View
from django.contrib.auth import logout
from django.http import HttpResponseRedirect
from django.utils import timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Module imports
from plane.authentication.provider.oidc import OIDCConfigurationError, OIDCProviderClient
from plane.authentication.utils.host import user_ip, base_host
from plane.db.models import User
from plane.authentication.services import record_sso_event
from plane.license.models import SSOProvider
from plane.utils.exception_logger import log_exception


def _end_session_url(endpoint, client_id, post_logout_redirect_uri):
    parsed = urlsplit(endpoint)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        [
            ("client_id", client_id),
            ("post_logout_redirect_uri", post_logout_redirect_uri),
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class SignOutAuthEndpoint(View):
    def post(self, request):
        local_redirect = base_host(request=request, is_app=True)
        provider_id = request.session.get("sso_provider_id")
        provider = SSOProvider.objects.filter(pk=provider_id).first() if provider_id else None
        user = request.user if request.user.is_authenticated else None
        try:
            if user is not None:
                User.objects.filter(pk=user.id).update(
                    last_logout_ip=user_ip(request=request),
                    last_logout_time=timezone.now(),
                )
            if provider is not None:
                record_sso_event(request, "logout", "success", provider=provider, actor=user)
        except Exception as exc:
            log_exception(exc)

        # Destroy the Plane session before any optional IdP request so local logout cannot depend on the IdP.
        logout(request)
        if provider is None:
            return HttpResponseRedirect(local_redirect)
        try:
            metadata = OIDCProviderClient(provider).get_discovery_document()
            endpoint = metadata.get("end_session_endpoint")
            if endpoint:
                return HttpResponseRedirect(_end_session_url(endpoint, provider.client_id, local_redirect))
        except OIDCConfigurationError as exc:
            log_exception(exc)
        return HttpResponseRedirect(local_redirect)
