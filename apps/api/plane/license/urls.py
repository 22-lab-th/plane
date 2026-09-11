# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.license.api.views import (
    EmailCredentialCheckEndpoint,
    InstanceAdminEndpoint,
    InstanceAdminSignInEndpoint,
    InstanceAdminSignUpEndpoint,
    InstanceConfigurationEndpoint,
    DisableEmailFeatureEndpoint,
    InstanceEndpoint,
    SignUpScreenVisitedEndpoint,
    InstanceAdminUserMeEndpoint,
    InstanceAdminSignOutEndpoint,
    InstanceAdminUserSessionEndpoint,
    InstanceWorkSpaceAvailabilityCheckEndpoint,
    InstanceWorkSpaceEndpoint,
    SSOProviderDetailEndpoint,
    SSOProviderEndpoint,
    SSOProviderInteractiveTestCallbackEndpoint,
    SSOProviderInteractiveTestInitiateEndpoint,
    SSOProviderRecoveryTestEndpoint,
    SSOProviderTestEndpoint,
)

urlpatterns = [
    path("", InstanceEndpoint.as_view(), name="instance"),
    path("admins/", InstanceAdminEndpoint.as_view(), name="instance-admins"),
    path("admins/me/", InstanceAdminUserMeEndpoint.as_view(), name="instance-admins"),
    path(
        "admins/session/",
        InstanceAdminUserSessionEndpoint.as_view(),
        name="instance-admin-session",
    ),
    path(
        "admins/sign-out/",
        InstanceAdminSignOutEndpoint.as_view(),
        name="instance-admins",
    ),
    path("admins/<uuid:pk>/", InstanceAdminEndpoint.as_view(), name="instance-admins"),
    path(
        "configurations/",
        InstanceConfigurationEndpoint.as_view(),
        name="instance-configuration",
    ),
    path(
        "configurations/disable-email-feature/",
        DisableEmailFeatureEndpoint.as_view(),
        name="disable-email-configuration",
    ),
    path(
        "admins/sign-in/",
        InstanceAdminSignInEndpoint.as_view(),
        name="instance-admin-sign-in",
    ),
    path(
        "admins/sign-up/",
        InstanceAdminSignUpEndpoint.as_view(),
        name="instance-admin-sign-in",
    ),
    path(
        "admins/sign-up-screen-visited/",
        SignUpScreenVisitedEndpoint.as_view(),
        name="instance-sign-up",
    ),
    path(
        "email-credentials-check/",
        EmailCredentialCheckEndpoint.as_view(),
        name="email-credential-check",
    ),
    path(
        "workspace-slug-check/",
        InstanceWorkSpaceAvailabilityCheckEndpoint.as_view(),
        name="instance-workspace-availability",
    ),
    path("workspaces/", InstanceWorkSpaceEndpoint.as_view(), name="instance-workspace"),
    path("sso/providers/", SSOProviderEndpoint.as_view(), name="sso-providers"),
    path("sso/providers/<uuid:pk>/", SSOProviderDetailEndpoint.as_view(), name="sso-provider-detail"),
    path("sso/providers/<uuid:pk>/test/", SSOProviderTestEndpoint.as_view(), name="sso-provider-test"),
    path(
        "sso/providers/<uuid:pk>/test-login/",
        SSOProviderInteractiveTestInitiateEndpoint.as_view(),
        name="sso-provider-interactive-test",
    ),
    path(
        "sso/providers/test-callback/",
        SSOProviderInteractiveTestCallbackEndpoint.as_view(),
        name="sso-provider-interactive-test-callback",
    ),
    path(
        "sso/providers/<uuid:pk>/test-recovery/",
        SSOProviderRecoveryTestEndpoint.as_view(),
        name="sso-provider-recovery-test",
    ),
]
