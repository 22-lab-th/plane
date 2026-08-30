# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.urls import path

from plane.api.views.page_asset import (
    ProjectPageAssetDetailAPIEndpoint,
    ProjectPageAssetListCreateAPIEndpoint,
)


urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/assets/",
        ProjectPageAssetListCreateAPIEndpoint.as_view(http_method_names=["get", "post"]),
        name="project-page-assets",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/assets/<uuid:asset_id>/",
        ProjectPageAssetDetailAPIEndpoint.as_view(http_method_names=["get", "patch", "delete"]),
        name="project-page-asset-detail",
    ),
]
