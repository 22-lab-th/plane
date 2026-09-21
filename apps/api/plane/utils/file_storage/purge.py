# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Purging a project file: every object first, then the audit event, then the row.

This module owns the one ordering that matters (ARCH-001 §4.4, R3-02):

1. delete **every** version object,
2. write the surviving ``purged`` audit row,
3. delete the file row (which takes its versions and links with it).

A row is therefore never removed while one of its objects still exists. If any
object deletion fails the file row is left in ``purge_failed`` - a state that
exists on the file row precisely so a failed purge is retried instead of dropped -
and the next run picks it up again regardless of age.

Both the ADMIN endpoint and the scheduled ``purge_expired_files`` task call
:func:`purge_file`, so the endpoint cannot invent a different order under time
pressure. Deletion is application-driven: the keys come from ``file_versions``
and the bucket is never listed (AD-12, AD-13).

The endpoint performs the purge **synchronously** and answers ``204``. ARCH-001
§4.4 words the endpoint as queuing a purge job; that hand-off is deliberately not
implemented while there is no worker path in this environment (the broker is
unreachable, and the smoke test asserts the post-conditions immediately after the
call), and the synchronous call keeps a single ordering shared with the task. It
is to be revisited when a worker path exists - the task already exists and only
needs to become the endpoint's executor.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Module imports
from plane.db.models import FileAccessLog, FileObject, FileVersion, Project
from plane.settings.storage import S3Storage
from plane.utils.exception_logger import log_exception
from plane.utils.file_storage import observability, quota as quota_module
from plane.utils.file_storage.quota import ACCOUNTED_VERSION_STATUSES
from plane.utils.file_storage.audit import record_file_access
from plane.utils.file_storage.retention import window_days

#: File-row states a purge may act on. ``purge_failed`` is a trashed file whose
#: earlier attempt could not delete an object and is being retried.
PURGEABLE_STATUSES = (FileObject.Status.TRASHED, FileObject.Status.PURGE_FAILED)

#: Rows one scheduled run handles, so a big backlog is drained in batches instead
#: of holding a worker (and its storage calls) for an unbounded time.
PURGE_BATCH_SIZE = 100


def purgeable_files(*, limit=PURGE_BATCH_SIZE):
    """Return the file rows the purge task owns, oldest first.

    Two groups, exactly as ARCH-001 §2.3 describes them: rows in ``trashed`` past
    their project's retention window (``Project.retention_days``, or
    ``PROJECT_FILE_TRASH_DAYS`` when the project does not set one - both through
    :func:`plane.utils.file_storage.retention.window_days`, which the restore
    refusal reads too) and **every** row already in ``purge_failed``, whose retry
    does not wait for a window that has already elapsed.

    The rows are read through ``all_objects`` because a trashed file *is*
    soft-deleted; the status filter is what makes the selection safe, and the
    per-project window is applied in one query by grouping projects by their
    effective number of days.
    """
    now = timezone.now()
    windows = Q(pk__in=[])

    grouped = {}
    for project_id, retention_days in Project.objects.values_list("id", "retention_days"):
        grouped.setdefault(window_days(retention_days), []).append(project_id)

    for days, project_ids in grouped.items():
        windows |= Q(project_id__in=project_ids, deleted_at__lte=now - timedelta(days=days))

    return list(
        FileObject.all_objects.filter(
            Q(status=FileObject.Status.PURGE_FAILED) | (Q(status=FileObject.Status.TRASHED) & windows)
        )
        .select_related("project")
        .order_by("deleted_at", "created_at")[:limit]
    )


def purge_file(file_object, *, request=None, trigger="manual"):
    """Purge one file; return ``True`` when the row is gone.

    ``False`` means an object could not be deleted: the file row was moved to
    ``purge_failed`` and nothing was removed, so the next attempt (or the next
    scheduled run) retries the same row. Object deletion is idempotent - deleting
    a key that is already gone succeeds - so an object that went missing between
    two attempts does not block the purge (R-DEL-4).
    """
    if file_object.status not in PURGEABLE_STATUSES:
        raise ValueError(f"refusing to purge a file in status {file_object.status!r}")

    storage = S3Storage(request=request)
    # The identifiers the records below carry are read before the row is removed:
    # reading them afterwards would re-query a row that no longer exists.
    workspace_id = file_object.project.workspace_id
    project_id = file_object.project_id
    file_id = file_object.id
    versions = list(file_object.versions.all())
    purged_bytes = sum(
        version.size_bytes or 0 for version in versions if version.status in ACCOUNTED_VERSION_STATUSES
    )

    for version in versions:
        if version.object_deleted_at is not None:
            # An earlier attempt (or the sweep) already removed this object.
            continue
        try:
            deleted = storage.delete_files([version.object_key])
        except Exception as exc:
            # ``delete_files`` reports a failed API call as ``False``, but a client
            # can also raise outside botocore's ``ClientError`` - a connection
            # error or a timeout, for instance. Both mean "this object is still
            # stored", so both become the retryable ``purge_failed`` state instead
            # of an unhandled 500 that leaves the row looking purgeable.
            log_exception(exc)
            deleted = False

        if not deleted:
            _mark_purge_failed(file_object, version)
            # The delete path's other verdict (AC-31): an object survived, so the
            # file stays in the trash and this attempt removed nothing.
            observability.record(
                observability.EVENT_DELETE,
                outcome=observability.DELETE_PURGE_FAILED,
                workspace_id=workspace_id,
                project_id=project_id,
                file_id=file_id,
                version_no=version.version_no,
                trigger=trigger,
                object_key=version.object_key,
            )
            return False
        version.mark_status(FileVersion.Status.PURGED, save=False)
        version.object_deleted_at = timezone.now()
        # A version whose object is gone must never carry the active pointer. The
        # purge can take the *active* version's object and then fail on another
        # object, so the file row survives while its active pointer names bytes that
        # no longer exist - and "the active version has no stored object" is the
        # state delivery must never be able to serve (T-104 F-4, asserted by the
        # versioning tests). A version with no object is not the active version.
        version.is_active = False
        version.save(
            update_fields=["status", "status_changed_at", "object_deleted_at", "is_active", "updated_at"]
        )

    with transaction.atomic():
        # Counters move inside the same transaction as the row removal, so a purge
        # never leaves usage pointing at bytes that are gone or bytes accounted for
        # by a row that no longer exists (AD-09).
        quota_row, usage_row = quota_module.lock_usage_rows(file_object.project)

        for version in versions:
            if version.reservation_released_at is None and (version.reserved_bytes or 0):
                quota_module.release(quota_row, usage_row, version)

        if purged_bytes:
            quota_module.subtract_used(quota_row, usage_row, used_bytes=purged_bytes)

        record_file_access(
            request,
            action=FileAccessLog.Action.PURGED,
            project=file_object.project,
            file_name=file_object.name_display,
            file_id=file_object.id,
            version_no=file_object.current_version_no or None,
            metadata={
                "trigger": trigger,
                "versions": len(versions),
                "bytes": purged_bytes,
                "object_keys": [version.object_key for version in versions],
            },
        )

        # Last: the row (and, by cascade, its versions and links). Audit rows are
        # not children of the file, so the trail survives this (AD-08, AC-27).
        FileObject.all_objects.filter(pk=file_object.pk).delete()

    # After the commit: the file and every one of its objects are gone, which is the
    # only state in which the delete is recorded as purged (AC-31).
    observability.record(
        observability.EVENT_DELETE,
        outcome=observability.DELETE_PURGED,
        workspace_id=workspace_id,
        project_id=project_id,
        file_id=file_id,
        version_no=file_object.current_version_no or None,
        trigger=trigger,
        versions=len(versions),
        bytes=purged_bytes,
    )

    return True


def _mark_purge_failed(file_object, version):
    """Record a failed attempt on the rows the next run will retry.

    The file row's display pointer is reconciled in the same step: the loop deletes
    newest first, so the version that failed may well not be the one the pointer
    names, and the pointer must never keep naming a version whose object this run
    removed (the detail response exposes it).
    """
    version.mark_status(FileVersion.Status.PURGE_FAILED)
    FileObject.all_objects.filter(pk=file_object.pk).update(
        status=FileObject.Status.PURGE_FAILED,
        updated_at=timezone.now(),
    )
    file_object.status = FileObject.Status.PURGE_FAILED
    file_object.reconcile_pointer()


def purge_expired_batch(*, limit=PURGE_BATCH_SIZE):
    """Purge up to ``limit`` rows the schedule owns; return a small result summary.

    Idempotent by construction: the selection is a status query, and a row that was
    purged (or made ``purge_failed`` by a failed attempt) is no longer in the same
    shape on the next run.
    """
    purged = 0
    failed = 0

    for file_object in purgeable_files(limit=limit):
        # One row's failure must not abort the batch: the row keeps its objects and
        # its status, so the next run retries it, while every other row is still
        # purged today rather than tomorrow.
        try:
            if purge_file(file_object, trigger="retention"):
                purged += 1
            else:
                failed += 1
        except Exception as exc:
            log_exception(exc)
            failed += 1

    return {"purged": purged, "failed": failed, "scanned": purged + failed}
