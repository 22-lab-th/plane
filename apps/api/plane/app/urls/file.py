# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    FileDetailEndpoint,
    FileDownloadEndpoint,
    FileListEndpoint,
    FilePreviewEndpoint,
    FileUploadAbortEndpoint,
    FileUploadCompleteEndpoint,
    FileUploadInitiateEndpoint,
)

urlpatterns = [
    # Listing (T-103)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/",
        FileListEndpoint.as_view(),
        name="project-files",
    ),
    # Upload lifecycle (T-102). The literal segments stay ahead of the
    # parameterised detail pattern so the intent is obvious.
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
    # Delivery (T-104)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/download/",
        FileDownloadEndpoint.as_view(),
        name="project-file-download",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/preview/",
        FilePreviewEndpoint.as_view(),
        name="project-file-preview",
    ),
    # Detail (T-103)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/",
        FileDetailEndpoint.as_view(),
        name="project-file-detail",
    ),
]
