# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    FileUploadAbortEndpoint,
    FileUploadCompleteEndpoint,
    FileUploadInitiateEndpoint,
)

urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/initiate-upload/",
        FileUploadInitiateEndpoint.as_view(),
        name="project-file-initiate-upload",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/complete-upload/",
        FileUploadCompleteEndpoint.as_view(),
        name="project-file-complete-upload",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/abort-upload/",
        FileUploadAbortEndpoint.as_view(),
        name="project-file-abort-upload",
    ),
]
