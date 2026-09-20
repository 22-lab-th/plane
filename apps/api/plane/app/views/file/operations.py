# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""File operations inside one project: rename, move and copy.

Three invariants shape this module:

* **A rename or a move never touches a key or an object.** Folders are virtual
  (AD-03) and the object key is write-once (AD-02), so ``name_original`` and
  ``object_key`` are byte-identical after a rename and no object is copied or
  deleted.
* **A copy mints a new identity.** The new file gets a new id and a new key built
  from *this* project's storage key prefix, and every version's object is copied
  server-side. The source keeps its own keys, so purging either file can never
  delete the other's bytes - two rows pointing at one object is the failure this
  design exists to prevent (ARCH-001 §2.3/§2.4).
* **A copy is charged like an upload.** The copied bytes go through the same
  reserve/settle helpers the upload lifecycle uses, so the project's usage grows
  by exactly what was copied and the ceiling is enforced before any object is
  duplicated.

Cross-project copy and move are T-122: ``target_project_id`` is refused here
rather than half-implemented, and nothing is aliased in the meantime.
"""

# Python imports
from uuid import uuid4

# Django imports
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.serializers.file import (
    FileCopySerializer,
    FileObjectSerializer,
    FileOperationSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.app.views.file.base import (
    GOOD_VERSION_STATUSES,
    file_for_write,
    available_display_name,
    folder_or_400,
    project_or_404,
    require_project_editor,
    stored_name,
)
from plane.db.models import FileAccessLog, FileObject, FileVersion
from plane.settings.storage import S3Storage
from plane.utils.file_storage import quota
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.naming import extension_of, normalize_name
from plane.utils.object_key import build_object_key


def _serialize_file(file_object):
    """Serialise one file with the link count the read endpoints also report."""
    file_object.link_count = file_object.links.count()
    return FileObjectSerializer(file_object).data


def patch_file(request, slug, project_id, file_id):
    """Rename, move and/or pin a file (ARCH-001 §4.1, AC-06/AC-10).

    A rename writes ``name_display``/``name_normalized`` only, and a move writes
    ``folder_id`` only; neither touches ``name_original`` or ``object_key``, and no
    storage call is made. A name the user chose for an existing file is refused
    when the destination folder already holds it (the folder endpoints' 409
    vocabulary), because here the user is making a choice rather than declaring a
    new upload - the upload and copy paths suffix instead (R-FOLD-5).
    """
    serializer = FileOperationSerializer(data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    payload = serializer.validated_data

    project = project_or_404(slug, project_id)
    require_project_editor(request, project)
    file_object = file_for_write(project, slug, file_id)

    rename_requested = "name_display" in payload
    move_requested = "folder_id" in request.data
    pin_requested = "is_pinned" in payload

    new_name = stored_name(payload.get("name_display") or file_object.name_display)
    new_folder = folder_or_400(project, request.data.get("folder_id")) if move_requested else file_object.folder
    renamed = rename_requested and new_name != file_object.name_display
    moved = move_requested and (new_folder.id if new_folder else None) != file_object.folder_id

    if not (renamed or moved or pin_requested):
        return Response({"file": _serialize_file(file_object)}, status=status.HTTP_200_OK)

    previous_name = file_object.name_display

    if pin_requested and not (renamed or moved):
        # Pinning is metadata with no action of its own in the audit vocabulary.
        FileObject.objects.filter(pk=file_object.pk).update(
            is_pinned=payload["is_pinned"],
            updated_at=timezone.now(),
        )
        file_object.refresh_from_db()
        return Response({"file": _serialize_file(file_object)}, status=status.HTTP_200_OK)

    try:
        with transaction.atomic():
            if renamed or moved:
                conflict = (
                    FileObject.objects.filter(
                        project_id=project.id,
                        folder_id=new_folder.id if new_folder else None,
                        name_normalized=normalize_name(new_name),
                    )
                    .exclude(pk=file_object.pk)
                    .exclude(status=FileObject.Status.TRASHED)
                    .exists()
                )
                if conflict:
                    raise ProjectFileError(
                        "A file with this name already exists in that folder.",
                        code="file_name_conflict",
                        status_code=status.HTTP_409_CONFLICT,
                        name_normalized=normalize_name(new_name),
                    )

                update_fields = ["updated_at"]
                if renamed:
                    file_object.name_display = new_name
                    file_object.name_normalized = normalize_name(new_name)
                    update_fields += ["name_display", "name_normalized"]
                if moved:
                    file_object.folder = new_folder
                    update_fields.append("folder")
                if pin_requested:
                    file_object.is_pinned = payload["is_pinned"]
                    update_fields.append("is_pinned")

                file_object.save(update_fields=update_fields)

                if renamed:
                    record_file_access(
                        request,
                        action=FileAccessLog.Action.RENAMED,
                        project=project,
                        file_name=new_name,
                        file_id=file_object.id,
                        version_no=file_object.current_version_no,
                        metadata={"previous_name": previous_name},
                    )
                if moved:
                    record_file_access(
                        request,
                        action=FileAccessLog.Action.MOVED,
                        project=project,
                        file_name=new_name,
                        file_id=file_object.id,
                        version_no=file_object.current_version_no,
                        metadata={"folder_id": str(new_folder.id) if new_folder else None},
                    )
    except IntegrityError:
        raise ProjectFileError(
            "A file with this name already exists in that folder.",
            code="file_name_conflict",
            status_code=status.HTTP_409_CONFLICT,
            name_normalized=normalize_name(new_name),
        )

    file_object.refresh_from_db()
    return Response({"file": _serialize_file(file_object)}, status=status.HTTP_200_OK)


class FileCopyEndpoint(BaseAPIView):
    """Copy a file into another folder of the same project (ARCH-001 §4.1).

    Every version's object is copied server-side (one Class A operation each) and
    the copy carries the version history, the source's category and no entity
    links; the bytes are charged to this project's quota through the reserve and
    settle helpers, and the source keeps its own keys untouched.

    ``target_project_id`` is refused: cross-project copy is T-122, which also owns
    the two-project permission check and the target-project charging rules.

    The endpoint shares the project-file upload throttle: a copy consumes quota
    and duplicates bytes, so it belongs to the same budget as an upload.
    """

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        serializer = FileCopySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        project = project_or_404(slug, project_id)
        require_project_editor(request, project)

        if payload.get("target_project_id"):
            raise ProjectFileError(
                "Copying to another project is not supported yet.",
                code="cross_project_not_supported",
                status_code=status.HTTP_400_BAD_REQUEST,
                field="target_project_id",
            )

        source = file_for_write(project, slug, file_id)
        folder = folder_or_400(project, payload.get("folder_id"))

        versions = list(
            source.versions.filter(
                status__in=GOOD_VERSION_STATUSES,
                object_deleted_at__isnull=True,
            ).order_by("version_no")
        )
        if not versions:
            raise ProjectFileError(
                "This file has no stored version to copy.",
                code="object_unavailable",
                status_code=status.HTTP_409_CONFLICT,
            )

        new_file_id = uuid4()
        new_name = stored_name(payload.get("name_display") or source.name_display)
        new_name = available_display_name(project, folder, new_name)
        storage_key = project.ensure_storage_key()

        # Each copied version gets its own key under this project's prefix and the
        # new file id, so no key is ever shared between two rows.
        plan = [
            (
                version,
                build_object_key(
                    workspace_slug=project.workspace.slug,
                    project_storage_key=storage_key,
                    category=source.category,
                    file_id=new_file_id,
                    version_no=version.version_no,
                    filename=new_name,
                    entity_ref=None,
                ),
            )
            for version in versions
        ]
        total_bytes = sum(version.size_bytes or 0 for version, _ in plan)
        # The copy takes the source's *active* version as its own active one. A
        # source with none has nothing a copy could be a copy of, so it is refused
        # here rather than falling back to the newest stored version: that fallback
        # would hand back a copy that downloads object bytes the source's own
        # pointer does not claim (the T-104 F-4 shape, on the write side now).
        active_source = next((version for version in versions if version.is_active), None)
        if active_source is None:
            raise ProjectFileError(
                "This file has no active version to copy.",
                code="object_unavailable",
                status_code=status.HTTP_409_CONFLICT,
                version_status=None,
            )

        key_by_version = {version.pk: object_key for version, object_key in plan}

        storage = S3Storage(request=request)
        copied_keys = []

        try:
            with transaction.atomic():
                quota_row, usage_row = quota.lock_usage_rows(project)
                # The ceiling is evaluated before a single byte is duplicated, and
                # the workspace row stays locked for the whole copy, so nothing can
                # take the capacity between the check and the counters.
                quota.ensure_within_limits(quota_row, usage_row, requested_bytes=total_bytes)

                for source_version, object_key in plan:
                    if storage.copy_object(source_version.object_key, object_key) is None:
                        raise ProjectFileError(
                            "The storage provider could not copy this object.",
                            code="storage_unavailable",
                            status_code=status.HTTP_502_BAD_GATEWAY,
                        )
                    copied_keys.append(object_key)

                file_object = FileObject(
                    id=new_file_id,
                    project=project,
                    folder=folder,
                    name_original=new_name,
                    name_display=new_name,
                    name_normalized=normalize_name(new_name),
                    mime_type=active_source.mime_type,
                    extension=extension_of(new_name),
                    size_bytes=active_source.size_bytes or 0,
                    checksum_sha256=active_source.client_checksum_sha256,
                    bucket=settings.AWS_STORAGE_BUCKET_NAME,
                    object_key=key_by_version[active_source.pk],
                    category=source.category,
                    status=FileObject.Status.ACTIVE,
                    visibility=source.visibility,
                    current_version_no=active_source.version_no,
                )
                file_object.save(force_insert=True, created_by_id=request.user.id)

                for source_version, object_key in plan:
                    is_active = source_version.pk == active_source.pk
                    version = FileVersion(
                        project=project,
                        file=file_object,
                        version_no=source_version.version_no,
                        object_key=object_key,
                        bucket=settings.AWS_STORAGE_BUCKET_NAME,
                        size_bytes=source_version.size_bytes or 0,
                        mime_type=source_version.mime_type,
                        client_checksum_sha256=source_version.client_checksum_sha256,
                        etag=source_version.etag,
                        magic_bytes_checked_at=source_version.magic_bytes_checked_at,
                        # The same attempt lifecycle as an upload: reserve, then
                        # settle, so usage moves from reserved to used exactly once.
                        status=FileVersion.Status.UPLOADING,
                        is_active=False,
                        storage_metadata={
                            **(source_version.storage_metadata or {}),
                            "copied_from": {
                                "file_id": str(source.id),
                                "object_key": source_version.object_key,
                            },
                        },
                    )
                    version.save(force_insert=True, created_by_id=request.user.id)
                    quota.reserve(
                        quota_row,
                        usage_row,
                        version,
                        requested_bytes=source_version.size_bytes or 0,
                        expires_at=timezone.now(),
                    )
                    quota.settle(
                        quota_row,
                        usage_row,
                        version,
                        observed_bytes=source_version.size_bytes or 0,
                        activate=is_active,
                    )

                record_file_access(
                    request,
                    action=FileAccessLog.Action.COPIED,
                    project=project,
                    file_name=new_name,
                    file_id=file_object.id,
                    version_no=active_source.version_no,
                    metadata={
                        "source_file_id": str(source.id),
                        "copied_versions": len(plan),
                        "folder_id": str(folder.id) if folder else None,
                    },
                )
        except Exception:
            # Objects are copied outside the database transaction's protection, so
            # a failed copy must not leave bytes nobody can see or purge.
            if copied_keys:
                storage.delete_files(copied_keys)
            raise

        return Response(
            {
                "file": _serialize_file(file_object),
                "source_file_id": str(source.id),
                "copied_versions": len(plan),
                "storage_usage": {
                    "project_used_bytes": usage_row.used_bytes,
                    "limit_bytes": usage_row.limit_bytes,
                },
            },
            status=status.HTTP_200_OK,
        )
