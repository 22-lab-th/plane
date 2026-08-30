# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.urls import path

from plane.api.views.view import ProjectViewDetailAPIEndpoint, ProjectViewListCreateAPIEndpoint


urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/",
        ProjectViewListCreateAPIEndpoint.as_view(http_method_names=["get", "post"]),
        name="project-views",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/<uuid:view_id>/",
        ProjectViewDetailAPIEndpoint.as_view(http_method_names=["get", "patch", "delete"]),
        name="project-view-detail",
    ),
]
