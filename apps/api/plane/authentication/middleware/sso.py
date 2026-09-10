# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.http import HttpResponseRedirect

from plane.authentication.adapter.error import AUTHENTICATION_ERROR_CODES, AuthenticationException
from plane.authentication.services import get_enforced_sso_provider
from plane.authentication.utils.host import base_host
from plane.utils.path_validator import get_safe_redirect_url

BLOCKED_AUTH_PATHS = (
    "/auth/sign-in/",
    "/auth/sign-up/",
    "/auth/magic-",
    "/auth/google/",
    "/auth/github/",
    "/auth/gitlab/",
    "/auth/gitea/",
    "/auth/forgot-password/",
    "/auth/reset-password/",
    "/auth/set-password/",
    "/auth/change-password/",
    "/auth/spaces/sign-in/",
    "/auth/spaces/sign-up/",
    "/auth/spaces/magic-",
    "/auth/spaces/google/",
    "/auth/spaces/github/",
    "/auth/spaces/gitlab/",
    "/auth/spaces/gitea/",
    "/auth/spaces/forgot-password/",
    "/auth/spaces/reset-password/",
)


class SSOEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(BLOCKED_AUTH_PATHS) and get_enforced_sso_provider() is not None:
            error = AuthenticationException(
                error_code=AUTHENTICATION_ERROR_CODES["SSO_AUTHENTICATION_FAILED"],
                error_message="SSO_AUTHENTICATION_FAILED",
            )
            next_path = request.GET.get("next_path") or request.POST.get("next_path")
            return HttpResponseRedirect(
                get_safe_redirect_url(
                    base_url=base_host(request=request, is_space=request.path.startswith("/auth/spaces/"), is_app=True),
                    next_path=next_path,
                    params=error.get_error_dict(),
                )
            )
        return self.get_response(request)
