# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    FileCopyEndpoint,
    FileVersionActivateEndpoint,
    FileVersionListEndpoint,
    FilePurgeEndpoint,
    FileRestoreEndpoint,
    FileDetailEndpoint,
    FileDownloadEndpoint,
    FileFolderDetailEndpoint,
    FileFolderListEndpoint,
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
    # Folder tree (T-105)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/folders/",
        FileFolderListEndpoint.as_view(),
        name="project-file-folders",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/folders/<uuid:folder_id>/",
        FileFolderDetailEndpoint.as_view(),
        name="project-file-folder-detail",
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
    # File operations (T-106) and the trash lifecycle (T-107)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/copy/",
        FileCopyEndpoint.as_view(),
        name="project-file-copy",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/restore/",
        FileRestoreEndpoint.as_view(),
        name="project-file-restore",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/purge/",
        FilePurgeEndpoint.as_view(),
        name="project-file-purge",
    ),
    # Versions (T-108): history, revision initiation and activation
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/versions/",
        FileVersionListEndpoint.as_view(),
        name="project-file-versions",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/versions/<int:version_no>/activate/",
        FileVersionActivateEndpoint.as_view(),
        name="project-file-version-activate",
    ),
    # Detail (T-103)
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/",
        FileDetailEndpoint.as_view(),
        name="project-file-detail",
    ),
]
