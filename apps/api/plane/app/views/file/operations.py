# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""File operations inside one project: rename, move, copy, trash, restore, purge.

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

Cross-project copy and move live in :mod:`plane.app.views.file.cross_project` (T-122)
and are built on :func:`copy_file_into_project` below, so the copy invariant is written
once and a cross-project copy cannot drift from an in-project one. ``target_project_id``
is **not** a field of these endpoints: the cross-project doors are separate routes, and
a client that sends the field here is refused rather than silently copied in place.
"""

# Python imports
from dataclasses import dataclass
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
    reject_unsupported_fields,
)
from plane.app.views.base import BaseAPIView
from plane.app.views.file.base import (
    TRASHED_STATUSES,
    available_display_name,
    file_for_write,
    file_queryset,
    folder_or_400,
    parse_bool,
    project_or_404,
    require_project_admin,
    require_project_editor,
    stored_name,
)
from plane.db.models import (
    FileAccessLog,
    FileFolder,
    FileLink,
    FileObject,
    FileVersion,
    Issue,
    IssueComment,
    Page,
    Project,
    ProjectStorageUsage,
)
from plane.settings.storage import S3Storage
from plane.throttles.project_file import ProjectFileUploadThrottle
from plane.utils.file_storage import quota
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.errors import ProjectFileError
from plane.utils.file_storage.verdicts import GOOD_VERSION_STATUSES
from plane.utils.file_storage.purge import purge_file as run_purge
from plane.utils.file_storage.retention import effective_retention_days, retention_has_expired
from plane.utils.exception_logger import log_exception
from plane.utils.file_storage.naming import extension_of, normalize_name
from plane.utils.object_key import build_object_key


def serialize_file(file_object):
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
        return Response({"file": serialize_file(file_object)}, status=status.HTTP_200_OK)

    previous_name = file_object.name_display

    if pin_requested and not (renamed or moved):
        # Pinning is metadata with no action of its own in the audit vocabulary.
        FileObject.objects.filter(pk=file_object.pk).update(
            is_pinned=payload["is_pinned"],
            updated_at=timezone.now(),
        )
        file_object.refresh_from_db()
        return Response({"file": serialize_file(file_object)}, status=status.HTTP_200_OK)

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
    return Response({"file": serialize_file(file_object)}, status=status.HTTP_200_OK)


@dataclass
class CopiedFile:
    """What one server-side copy into a project produced."""

    #: The new row, in the project the bytes were copied into.
    file: FileObject
    #: The row it was copied from.
    source: FileObject
    #: ``[(source version, new object key)]`` in version order: the whole plan, so a
    #: caller can verify or discard exactly what this copy wrote.
    plan: list
    #: The target project's counters after settlement.
    usage: ProjectStorageUsage
    #: The version number the copy's active pointer names (the source's own active one).
    active_version_no: int


def copy_file_into_project(*, source, target_project, folder, name, request, audit=None, verify=None):
    """Copy every stored version of ``source`` into ``target_project`` and charge it.

    The one implementation of the copy invariant (ARCH-001 §2.3/§2.4), shared by the
    in-project copy and by the cross-project copy and move doors:

    * every version's object is copied **server-side** (one Class A call each) to a new
      key built from the *target* project's prefix and the new file id, so no key is
      ever shared between two rows and purging either file cannot delete the other's
      bytes;
    * the copied bytes go through the same reserve/settle helpers an upload uses,
      charged to the target project under the target's usage lock, so a ceiling refuses
      the copy before a single byte is duplicated;
    * the new file carries the source's category, version history and active pointer,
      and **no entity links** - links are project-scoped, so a copy never carries them;
    * a source with no active version is refused rather than falling back to the newest
      stored version (T-106 F-1): that fallback would hand back a copy whose pointer
      downloads bytes the source's own pointer does not claim.

    :param name: the display name asked for; it is sanitised and suffixed until it is
        free in ``folder`` (R-FOLD-5), exactly as an upload's would be.
    :param audit: called once **inside** the copy's transaction with the
        :class:`CopiedFile`; each door writes the audit rows it owes there, so a copy is
        never committed without its trail.
    :param verify: called with the storage client and the :class:`CopiedFile` before
        the transaction commits - the cross-project **move** passes the HEAD
        verification here, so a copy that does not match its source rolls back (rows,
        counters, audit rows) and its objects are deleted, and the source is never
        touched (AC-42).
    """
    versions = list(
        source.versions.filter(status__in=GOOD_VERSION_STATUSES, object_deleted_at__isnull=True).order_by(
            "version_no"
        )
    )
    if not versions:
        raise ProjectFileError(
            "This file has no stored version to copy.",
            code="object_unavailable",
            status_code=status.HTTP_409_CONFLICT,
        )

    active_source = next((version for version in versions if version.is_active), None)
    if active_source is None:
        raise ProjectFileError(
            "This file has no active version to copy.",
            code="object_unavailable",
            status_code=status.HTTP_409_CONFLICT,
            version_status=None,
        )

    new_file_id = uuid4()
    new_name = available_display_name(target_project, folder, stored_name(name))
    storage_key = target_project.ensure_storage_key()

    # Each copied version gets its own key under the target project's prefix and the
    # new file id, so no key is ever shared between two rows.
    plan = [
        (
            version,
            build_object_key(
                workspace_slug=target_project.workspace.slug,
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
    key_by_version = {version.pk: object_key for version, object_key in plan}

    storage = S3Storage(request=request)
    copied_keys = []

    try:
        with transaction.atomic():
            quota_row, usage_row = quota.lock_usage_rows(target_project)
            # The ceiling is evaluated before a single byte is duplicated, and the
            # workspace row stays locked for the whole copy, so nothing can take the
            # capacity between the check and the counters.
            quota.ensure_within_limits(quota_row, usage_row, requested_bytes=total_bytes)

            for source_version, object_key in plan:
                # ``copy_object`` reports a failed call as ``None``, but the client can
                # also raise outside ``ClientError`` (a connection error or a timeout);
                # both mean the copy did not happen, so both answer the same 502 instead
                # of an unhandled 500.
                try:
                    copied = storage.copy_object(source_version.object_key, object_key)
                except Exception as exc:
                    log_exception(exc)
                    copied = None

                if copied is None:
                    raise ProjectFileError(
                        "The storage provider could not copy this object.",
                        code="storage_unavailable",
                        status_code=status.HTTP_502_BAD_GATEWAY,
                    )
                copied_keys.append(object_key)

            file_object = FileObject(
                id=new_file_id,
                project=target_project,
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
                    project=target_project,
                    file=file_object,
                    version_no=source_version.version_no,
                    object_key=object_key,
                    bucket=settings.AWS_STORAGE_BUCKET_NAME,
                    size_bytes=source_version.size_bytes or 0,
                    mime_type=source_version.mime_type,
                    client_checksum_sha256=source_version.client_checksum_sha256,
                    etag=source_version.etag,
                    magic_bytes_checked_at=source_version.magic_bytes_checked_at,
                    # The same attempt lifecycle as an upload: reserve, then settle, so
                    # usage moves from reserved to used exactly once.
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

            copied_file = CopiedFile(
                file=file_object,
                source=source,
                plan=plan,
                usage=usage_row,
                active_version_no=active_source.version_no,
            )

            if verify is not None:
                verify(storage, copied_file)

            if audit is not None:
                audit(copied_file)

            return copied_file
    except Exception:
        # Objects are copied outside the database transaction's protection, so a failed
        # copy must not leave bytes nobody can see or purge - the same guard covers a
        # verification refusal, whose rows the transaction rolls back. The rollback is
        # best-effort by necessity (the original failure is what the caller must see),
        # but a provider that refuses the delete leaves orphan bytes with no row, so that
        # verdict is said out loud instead of swallowed.
        if copied_keys and not storage.delete_files(copied_keys):
            log_exception(
                RuntimeError(
                    f"the rollback of a failed copy could not delete {len(copied_keys)} object(s); they are "
                    "stored under the target project's prefix with no row naming them"
                )
            )
        raise


class FileCopyEndpoint(BaseAPIView):
    """Copy a file into another folder of the same project (ARCH-001 §4.1).

    Every version's object is copied server-side (one Class A operation each) and the
    copy carries the version history, the source's category and no entity links; the
    bytes are charged to this project's quota through the reserve and settle helpers,
    and the source keeps its own keys untouched.

    ``target_project_id`` is not accepted here: another project is the
    ``copy-to-project/`` route's job, and this endpoint answers the field it cannot
    honour rather than dropping it into an in-project copy (T-106 F-3).

    The endpoint shares the project-file upload throttle: a copy consumes quota and
    duplicates bytes, so it belongs to the same budget as an upload.
    """

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        if request.data.get("target_project_id"):
            raise ProjectFileError(
                "This endpoint copies inside one project; send a cross-project copy to the "
                "copy-to-project route instead.",
                code="unsupported_field",
                status_code=status.HTTP_400_BAD_REQUEST,
                field="target_project_id",
            )

        serializer = FileCopySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        project = project_or_404(slug, project_id)
        require_project_editor(request, project)
        source = file_for_write(project, slug, file_id)
        folder = folder_or_400(project, payload.get("folder_id"))

        def audit(copied):
            record_file_access(
                request,
                action=FileAccessLog.Action.COPIED,
                project=project,
                file_name=copied.file.name_display,
                file_id=copied.file.id,
                version_no=copied.active_version_no,
                metadata={
                    "source_file_id": str(source.id),
                    "copied_versions": len(copied.plan),
                    "folder_id": str(folder.id) if folder else None,
                },
            )

        copied = copy_file_into_project(
            source=source,
            target_project=project,
            folder=folder,
            name=payload.get("name_display") or source.name_display,
            request=request,
            audit=audit,
        )

        return Response(
            {
                "file": serialize_file(copied.file),
                "source_file_id": str(source.id),
                "copied_versions": len(copied.plan),
                "storage_usage": {
                    "project_used_bytes": copied.usage.used_bytes,
                    "limit_bytes": copied.usage.limit_bytes,
                },
            },
            status=status.HTTP_200_OK,
        )


#: Entity types this distribution can check for liveness when a link is revived.
#: A type with no local table here (milestone, deliverable) is revived: nothing in
#: this build can contradict the link, and dropping it would lose a real binding.
LIVE_ENTITY_MODELS = {
    FileLink.EntityType.PROJECT: Project,
    FileLink.EntityType.ISSUE: Issue,
    FileLink.EntityType.PAGE: Page,
    FileLink.EntityType.COMMENT: IssueComment,
}


def _entity_is_live(link):
    """True when the entity a link points at still exists."""
    model = LIVE_ENTITY_MODELS.get(link.entity_type)
    if model is None:
        return True

    return model.objects.filter(id=link.entity_id).exists()


def _trashed_file_or_refuse(project, slug, file_id):
    """Resolve a file for a trash operation, or refuse with the documented code.

    Both ``restore`` and ``purge`` address rows the default surface hides, so they
    read through ``file_queryset`` with the trash included - the same visibility
    source the list and detail endpoints use, never a manager of their own - and
    refuse anything that is not actually in the trash.
    """
    file_object = file_queryset(project, slug, include_trashed=True).get(id=file_id)

    if file_object.status not in TRASHED_STATUSES:
        raise ProjectFileError(
            "This file is not in the trash.",
            code="file_not_trashed",
            status_code=status.HTTP_409_CONFLICT,
        )

    return file_object


def _response_file(file_object):
    """Serialise the file as the endpoints return it (link count included)."""
    file_object.refresh_from_db()
    return serialize_file(file_object)


def trash_file_row(file_object, *, request, project):
    """Mark one file trashed, unlink its links, and return the ids it unlinked.

    Both markers move together - ``status='trashed'`` and ``deleted_at`` - because the
    shared visibility predicate reads both, and the versions and their objects are
    untouched: trashed files keep consuming quota until they are purged (AD-09), which
    is why no counter moves here. Links are unlinked so an issue stops showing a deleted
    attachment; their rows survive so a restore can revive the ones whose entity is
    still there.

    Shared by the trash endpoint and the cross-project move (T-122), which must purge
    the source only after the copied object verified - and which leaves the source in
    exactly this state (recoverable) when that purge fails (R-OPS-4).
    """
    links = list(file_object.links.all())
    versions = file_object.versions.count()
    now = timezone.now()

    with transaction.atomic():
        # The links are marked inactive with an update rather than the model's
        # ``delete()``: that helper queues a deferred hard-delete job through the
        # broker, and trashing a file must not depend on a worker being reachable.
        # The rows stay, so a restore can revive the links whose entity is alive.
        if links:
            FileLink.objects.filter(id__in=[link.id for link in links]).update(deleted_at=now, updated_at=now)

        FileObject.all_objects.filter(pk=file_object.pk).update(
            status=FileObject.Status.TRASHED,
            deleted_at=now,
            updated_at=now,
        )

        record_file_access(
            request,
            action=FileAccessLog.Action.TRASHED,
            project=project,
            file_name=file_object.name_display,
            file_id=file_object.id,
            metadata={
                # The restore path reads these two back: which links this trash
                # unlinked, and what the row's status was before it moved.
                "link_ids": [str(link.id) for link in links],
                "previous_status": file_object.status,
                "folder_id": str(file_object.folder_id) if file_object.folder_id else None,
                "versions": versions,
            },
        )

    # The caller may purge the row immediately (the move door does), and ``purge_file``
    # refuses anything whose in-memory status is not purgeable.
    file_object.status = FileObject.Status.TRASHED
    file_object.deleted_at = now
    return [link.id for link in links]


def trash_file(request, slug, project_id, file_id):
    """Move a file to the trash (R-DEL-1, AC-11)."""
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)

    file_object = file_for_write(project, slug, file_id)
    trash_file_row(file_object, request=request, project=project)

    return Response(status=status.HTTP_204_NO_CONTENT)


def restore_file(request, slug, project_id, file_id):
    """Restore a trashed file (R-DEL-2, AC-11).

    Restoring clears both markers and returns the row to the status it had before
    the trash. The folder is re-checked: a recursive folder delete trashes the
    whole subtree, so a nested file whose folder is gone lands at the project root
    instead of pointing at a hidden folder, and the audit row records which of the
    two happened. Links the trash unlinked are revived when their entity is still
    live; a link whose entity is gone stays unlinked.

    The other half of the retention window is enforced here (R-DEL-2's edge case,
    R-LIFE-1's "purge wins after the window"): a file whose window has elapsed is
    already selected by the purge and cannot be brought back. A ``purge_failed`` row
    inside its window is deliberately *not* refused - a partial purge is repairable,
    which is what the delivery invariant is checked on (the activation refuses a
    version whose bytes are gone), not what the status alone says.
    """
    project = project_or_404(slug, project_id)
    require_project_editor(request, project)

    file_object = _trashed_file_or_refuse(project, slug, file_id)

    if retention_has_expired(file_object):
        raise ProjectFileError(
            "This file's retention window has elapsed, so it can no longer be restored.",
            code="retention_expired",
            status_code=status.HTTP_409_CONFLICT,
            file_id=str(file_object.id),
            retention_days=effective_retention_days(project),
        )

    trash_audit = (
        FileAccessLog.objects.filter(file_id=file_object.id, action=FileAccessLog.Action.TRASHED)
        .order_by("-created_at")
        .first()
    )
    trash_metadata = (trash_audit.metadata or {}) if trash_audit is not None else {}

    folder = None
    folder_fallback = False
    if file_object.folder_id is not None:
        folder = FileFolder.objects.filter(id=file_object.folder_id, project_id=project.id).first()
        folder_fallback = folder is None

    previous_status = trash_metadata.get("previous_status")
    if previous_status not in FileObject.Status.values or previous_status in (
        FileObject.Status.TRASHED,
        FileObject.Status.PURGE_FAILED,
    ):
        previous_status = FileObject.Status.ACTIVE

    restored_links = 0
    try:
        with transaction.atomic():
            for raw_link_id in trash_metadata.get("link_ids", []):
                link = FileLink.all_objects.filter(id=raw_link_id, file_id=file_object.id).first()
                if link is None or link.deleted_at is None or not _entity_is_live(link):
                    continue
                FileLink.all_objects.filter(pk=link.pk).update(deleted_at=None, updated_at=timezone.now())
                restored_links += 1

            FileObject.all_objects.filter(pk=file_object.pk).update(
                status=previous_status,
                deleted_at=None,
                folder=folder,
                updated_at=timezone.now(),
            )

            record_file_access(
                request,
                action=FileAccessLog.Action.RESTORED,
                project=project,
                file_name=file_object.name_display,
                file_id=file_object.id,
                metadata={
                    "restored_links": restored_links,
                    "folder_fallback": folder_fallback,
                    "folder_id": str(folder.id) if folder is not None else None,
                },
            )
    except IntegrityError:
        # The partial unique keeps one live name per folder: if something took the
        # name while the file sat in the trash, the caller has to decide, exactly as
        # a rename does - the same vocabulary, not a silent rename.
        raise ProjectFileError(
            "A file with this name already exists in that folder.",
            code="file_name_conflict",
            status_code=status.HTTP_409_CONFLICT,
            name_normalized=file_object.name_normalized,
        )

    return Response(
        {
            "file": _response_file(file_object),
            "restore": {"restored_links": restored_links, "folder_fallback": folder_fallback},
        },
        status=status.HTTP_200_OK,
    )


class FileRestoreEndpoint(BaseAPIView):
    """Restore a file from the trash (ARCH-001 §4.1)."""

    throttle_classes = [ProjectFileUploadThrottle]

    def post(self, request, slug, project_id, file_id):
        return restore_file(request, slug, project_id, file_id)


class FilePurgeEndpoint(BaseAPIView):
    """Purge a file for good: project ADMIN only, explicit confirmation (AC-12, AC-27).

    The work itself is :func:`plane.utils.file_storage.purge.purge_file`, which the
    scheduled task calls too - the endpoint must not have its own ordering, because
    the ordering ("every object first, then the audit event, then the row") is what
    keeps a row from disappearing while an object survives (R3-02).

    The purge is performed synchronously and answered with ``204``. ARCH-001 §4.4
    words this endpoint as queuing a purge job; the hand-off is deliberately not
    implemented while this environment has no worker path (the broker is
    unreachable and the smoke test asserts the post-conditions immediately after
    the call), so one ordering is shared with the task instead of two. Revisit when
    a worker path exists: the task is already the executor.
    """

    throttle_classes = [ProjectFileUploadThrottle]

    def delete(self, request, slug, project_id, file_id):
        project = project_or_404(slug, project_id)
        require_project_admin(request, project)

        reject_unsupported_fields(request.data, ("confirm",))

        raw_confirm = request.query_params.get("confirm")
        if raw_confirm in (None, ""):
            raw_confirm = request.data.get("confirm")
        if raw_confirm in (None, ""):
            raise ProjectFileError(
                "Purging a file is irreversible; confirm it with confirm=true.",
                code="confirmation_required",
                status_code=status.HTTP_400_BAD_REQUEST,
                field="confirm",
            )
        if not parse_bool(raw_confirm, "confirm"):
            raise ProjectFileError(
                "Purging a file is irreversible; confirm it with confirm=true.",
                code="confirmation_required",
                status_code=status.HTTP_400_BAD_REQUEST,
                field="confirm",
            )

        file_object = _trashed_file_or_refuse(project, slug, file_id)

        if not run_purge(file_object, request=request, trigger="manual"):
            raise ProjectFileError(
                "The storage provider could not delete every object; the file stays in the trash and the "
                "purge will be retried.",
                code="storage_unavailable",
                status_code=status.HTTP_502_BAD_GATEWAY,
                file_id=str(file_object.id),
            )

        return Response(status=status.HTTP_204_NO_CONTENT)
