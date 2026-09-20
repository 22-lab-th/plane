# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Folder tree operations: create, rename, move and recursive delete.

Folders are virtual: they live in the database only and never appear in an object
key, so renaming or moving one never rewrites a stored object (AD-03, R-FOLD-3).
The tree invariants enforced here are the ones the schema cannot express:

* a folder may not be moved into itself or its own descendant (a cycle);
* the denormalised ``depth`` stays correct for the whole moved subtree and never
  exceeds :data:`MAX_FOLDER_DEPTH`;
* deleting a folder moves its files to trash in the same transaction and never
  hard-deletes anything or silently unlinks a file (R-FOLD-4).

Name collisions are refused with a machine-readable 409 rather than suffixed
(R-FOLD-5 allows either, and the UI asks the user, which keeps breadcrumbs and
paths predictable). The unique index covers the project root as well, because the
constraint was built with ``nulls_distinct=False`` (ARCH-001 §2.2).
"""

# Django imports
from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.serializers.file import FileFolderSerializer, FileFolderWriteSerializer
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    breadcrumbs,
    parse_bool,
    project_or_404,
    require_project_editor,
)
from plane.db.models import FileAccessLog, FileFolder, FileObject
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.naming import normalize_name

#: Deepest folder the tree accepts, matching the denormalised ``depth`` column
#: (ARCH-001 §2.2). The project root is depth 0.
MAX_FOLDER_DEPTH = 32


def folder_or_404(project, folder_id):
    """Return the folder inside this project; a foreign folder is not found."""
    return FileFolder.objects.get(id=folder_id, project_id=project.id)


def parent_folder(project, parent_id):
    """Resolve a requested parent, ``None`` meaning the project root."""
    if parent_id in (None, "", "null"):
        return None

    return folder_or_404(project, parent_id)


def require_depth(depth):
    """Refuse a folder that would sit deeper than the tree allows."""
    if depth > MAX_FOLDER_DEPTH:
        raise ProjectFileError(
            f"Folders may not be nested deeper than {MAX_FOLDER_DEPTH} levels.",
            code="depth_limit_exceeded",
            status_code=status.HTTP_409_CONFLICT,
            depth=depth,
            max_depth=MAX_FOLDER_DEPTH,
        )


def descendant_ids(project, folder):
    """Return the ids of every descendant, level by level.

    The walk is bounded by :data:`MAX_FOLDER_DEPTH`, so even a corrupted cycle in
    the data cannot make it run away, and duplicates from such a cycle collapse
    into a set.
    """
    descendants = set()
    frontier = [folder.id]

    for _ in range(MAX_FOLDER_DEPTH + 2):
        children = list(
            FileFolder.objects.filter(project_id=project.id, parent_id__in=frontier).values_list("id", flat=True)
        )
        if not children:
            break

        descendants.update(children)
        frontier = children

    return list(descendants)


def is_self_or_descendant(project, folder, candidate_id):
    """Return True when ``candidate_id`` is ``folder`` itself or below it."""
    parents = dict(FileFolder.objects.filter(project_id=project.id).values_list("id", "parent_id"))

    current = candidate_id
    visited = set()
    while current is not None and current not in visited:
        if current == folder.id:
            return True
        visited.add(current)
        current = parents.get(current)

    return False


def folder_payload(project, folder):
    """The folder plus its breadcrumb path, in the shape the listing returns."""
    return {"folder": FileFolderSerializer(folder).data, "breadcrumbs": breadcrumbs(project, folder)}


class FileFolderListEndpoint(BaseAPIView):
    """Create a folder (ARCH-001 §4.1)."""

    def post(self, request, slug, project_id):
        serializer = FileFolderWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project = project_or_404(slug, project_id)
        require_project_editor(request, project)

        name = serializer.validated_data["name"]
        parent = parent_folder(project, serializer.validated_data.get("parent_id"))
        depth = (parent.depth + 1) if parent is not None else 0
        require_depth(depth)

        folder = FileFolder(
            project=project,
            parent=parent,
            name=name,
            name_normalized=normalize_name(name),
            depth=depth,
        )
        try:
            with transaction.atomic():
                folder.save(force_insert=True, created_by_id=request.user.id)
        except IntegrityError:
            raise ProjectFileError(
                "A folder with this name already exists here.",
                code="folder_name_conflict",
                status_code=status.HTTP_409_CONFLICT,
                name_normalized=folder.name_normalized,
            )

        return Response(folder_payload(project, folder), status=status.HTTP_200_OK)


class FileFolderDetailEndpoint(BaseAPIView):
    """Rename, move or recursively delete a folder (ARCH-001 §4.1)."""

    def patch(self, request, slug, project_id, folder_id):
        serializer = FileFolderWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        project = project_or_404(slug, project_id)
        require_project_editor(request, project)
        folder = folder_or_404(project, folder_id)

        name = serializer.validated_data.get("name") or folder.name
        parent_moved = "parent_id" in request.data
        parent = (
            parent_folder(project, serializer.validated_data.get("parent_id")) if parent_moved else folder.parent
        )

        try:
            with transaction.atomic():
                if parent_moved:
                    if parent is not None and is_self_or_descendant(project, folder, parent.id):
                        raise ProjectFileError(
                            "A folder cannot be moved into itself or its own descendant.",
                            code="folder_cycle",
                            status_code=status.HTTP_409_CONFLICT,
                            folder_id=str(folder.id),
                        )

                    new_depth = (parent.depth + 1) if parent is not None else 0
                    delta = new_depth - folder.depth
                    if delta:
                        descendants = descendant_ids(project, folder)
                        require_depth(new_depth)
                        if descendants:
                            # Every descendant shifts by the same delta, so one
                            # statement keeps the whole subtree consistent.
                            deepest = (
                                FileFolder.objects.filter(id__in=descendants).aggregate(deepest=Max("depth"))[
                                    "deepest"
                                ]
                                or folder.depth
                            )
                            require_depth(deepest + delta)
                            FileFolder.objects.filter(id__in=descendants).update(depth=F("depth") + delta)

                    folder.parent = parent
                    folder.depth = new_depth

                folder.name = name
                folder.name_normalized = normalize_name(name)
                folder.save(update_fields=["parent", "depth", "name", "name_normalized", "updated_at"])
        except IntegrityError:
            raise ProjectFileError(
                "A folder with this name already exists in the destination folder.",
                code="folder_name_conflict",
                status_code=status.HTTP_409_CONFLICT,
                name_normalized=folder.name_normalized,
            )

        return Response(folder_payload(project, folder), status=status.HTTP_200_OK)

    def delete(self, request, slug, project_id, folder_id):
        recursive = False
        raw_recursive = request.query_params.get("recursive")
        if raw_recursive in (None, "") and isinstance(request.data, dict):
            raw_recursive = request.data.get("recursive")
        if raw_recursive not in (None, ""):
            recursive = parse_bool(raw_recursive, "recursive")

        project = project_or_404(slug, project_id)
        require_project_editor(request, project)
        folder = folder_or_404(project, folder_id)

        subtree = [*descendant_ids(project, folder), folder.id]

        with transaction.atomic():
            # The emptiness decision is made inside the transaction, so a file
            # uploaded while the delete is in flight is either seen here (and the
            # delete is refused without recursive=true) or lands in an already
            # deleted folder (and the upload itself fails).
            child_folders = FileFolder.objects.filter(project_id=project.id, parent_id=folder.id).count()
            live_files = FileObject.objects.filter(project_id=project.id, folder_id__in=subtree).exclude(
                status=FileObject.Status.TRASHED
            )

            if not recursive:
                file_count = live_files.count()
                if child_folders or file_count:
                    raise ProjectFileError(
                        "This folder is not empty; pass recursive=true to move its contents to trash.",
                        code="folder_not_empty",
                        status_code=status.HTTP_409_CONFLICT,
                        folder_count=child_folders,
                        file_count=file_count,
                    )

            trashed_files = list(live_files)
            deleted_at = timezone.now()

            if trashed_files:
                # Trash, never purge: the objects stay in the bucket until the
                # retention job removes them (AD-09, R-FOLD-4).
                live_files.update(
                    status=FileObject.Status.TRASHED,
                    deleted_at=deleted_at,
                    updated_at=deleted_at,
                )

            FileFolder.objects.filter(id__in=subtree).update(deleted_at=deleted_at, updated_at=deleted_at)

            for file_object in trashed_files:
                record_file_access(
                    request,
                    action=FileAccessLog.Action.TRASHED,
                    project=project,
                    file_name=file_object.name_display,
                    file_id=file_object.id,
                    version_no=file_object.current_version_no,
                    metadata={"folder_id": str(folder.id), "via": "folder_delete"},
                )

        return Response(status=status.HTTP_204_NO_CONTENT)
