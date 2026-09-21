# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.app.views import (
    FileActivityEndpoint,
    FileCopyEndpoint,
    FileCopyToProjectEndpoint,
    FileMoveToProjectEndpoint,
    FileStorageEndpoint,
    FileEntityLinkListEndpoint,
    FileLinkDetailEndpoint,
    FileLinkListEndpoint,
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
    # Cross-project copy and move (T-122). Separate routes rather than a field on
    # ``copy/``: each one authorizes two projects, charges the target and returns its
    # own payload, so the doors are not the same operation.
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/copy-to-project/",
        FileCopyToProjectEndpoint.as_view(),
        name="project-file-copy-to-project",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/move-to-project/",
        FileMoveToProjectEndpoint.as_view(),
        name="project-file-move-to-project",
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
    # Audit trail (T-111). A literal segment, declared before the parameterised
    # detail patterns.
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/activity/",
        FileActivityEndpoint.as_view(),
        name="project-file-activity",
    ),
    # Storage usage (T-110). A literal segment, so it is declared before the
    # parameterised detail patterns.
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/storage/",
        FileStorageEndpoint.as_view(),
        name="project-file-storage",
    ),
    # Entity links (T-109), and the entity -> file direction T-115's issue and
    # page surfaces read. The literal segment stays ahead of the parameterised
    # detail pattern so it is never taken for a file id.
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/links/",
        FileEntityLinkListEndpoint.as_view(),
        name="project-file-entity-links",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/links/",
        FileLinkListEndpoint.as_view(),
        name="project-file-links",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/files/<uuid:file_id>/links/<uuid:link_id>/",
        FileLinkDetailEndpoint.as_view(),
        name="project-file-link-detail",
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
