# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Deleting the objects that never became a verified version (AC-20, ARCH-001 §4.4).

Two scheduled jobs live here; the Celery wrappers in
:mod:`plane.bgtasks.file_sweep_task` only name them and the beat entries only say
when to run them, so a test can call exactly what the schedule calls.

* :func:`cleanup_unverified_batch` is **the** deletion mechanism for objects with no
  verified version - abandoned uploads, over-sized ones and ones that failed
  verification. The predicate is evaluated against the database and never by listing
  the bucket (AD-13: a prefix listing could not reach every category anyway), and
  every branch carries an age guard, because deleting at the URL's expiry would race
  a PUT that started just before it (the margin).
* :func:`recheck_deleted_batch` is the **resurrection guard**. Timing arguments
  cannot close a PUT that starts inside the URL's lifetime and finishes after the
  sweep removed the object, so a marked row whose object is found again by a HEAD is
  deleted again; ``resurrection_detected`` is the counter that says the margin needs
  widening.

Both jobs are batch-bounded and idempotent: the sweep's selection is
``object_deleted_at IS NULL`` (its own terminal marker, §2.4), the re-check's is "the
marker is over 24 h old and the row is still non-active", so a second run over the
same data does nothing twice. A reservation is released through the shared guarded
single-fire statement, so whichever actor ends the attempt first - the abort
endpoint, a failed finalize or this sweep - is the only one that moves a counter.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Module imports
from plane.db.models import FileObject, FileVersion
from plane.settings.storage import S3Storage
from plane.utils.exception_logger import log_exception
from plane.utils.file_storage import observability, quota as quota_module

#: Versions one sweep run handles, oldest first, so a backlog drains over several
#: runs instead of holding a worker and its storage calls (ARCH-001 §4.4).
SWEEP_BATCH_SIZE = 500

#: Rows one re-check run HEADs. Smaller than the sweep's batch because every row
#: costs a Class B call against the bucket.
RECHECK_BATCH_SIZE = 100

#: How long after the sweep removed an object a row stays worth re-checking. A PUT
#: that outlives the sweep can only be one that started before the URL expired and
#: finished after the deletion, so the window is generous by design (§2.4).
RESURRECTION_WINDOW = timedelta(hours=24)

#: The version states that hold no verified object. ``active``/``superseded`` are
#: verified by definition, and ``purged``/``purge_failed`` belong to the purge
#: (R-DEL-4) rather than here, so neither is swept by this job.
UNVERIFIED_STATUSES = (FileVersion.Status.UPLOADING, FileVersion.Status.FAILED)


def sweep_margin():
    """The margin added to the upload URL's TTL before an attempt may be taken.

    A presigned URL expires exactly at ``created_at + TTL`` and the provider
    validates the signature when the *request starts*, so deleting at the TTL would
    race a PUT that began a moment before it. Deleting only after the TTL plus this
    margin is what removes that race (ARCH-001 §4.4).
    """
    return timedelta(seconds=settings.PROJECT_FILE_SWEEP_MARGIN_SECONDS)


def unverified_versions(*, limit=SWEEP_BATCH_SIZE):
    """The version rows the sweep owns, oldest first (ARCH-001 §4.4).

    Two branches, both age-guarded and both inside ``object_deleted_at IS NULL`` so
    a swept row is never selected twice:

    * a reservation that has expired past the margin - the attempt is abandoned and
      nothing legitimate can still store into it;
    * an attempt whose ``status_changed_at`` is older than the upload URL's TTL plus
      the margin - this is the branch that also covers a ``failed`` row, which has no
      live reservation left but whose object is exactly what the sweep exists to
      remove ("Failure ... leaves ``object_deleted_at`` null so the sweep owns the
      object", §2.4).
    """
    now = timezone.now()
    margin = sweep_margin()
    abandoned = Q(
        status=FileVersion.Status.UPLOADING,
        reservation_expires_at__lt=now - margin,
    )
    stale_attempt = Q(
        status__in=UNVERIFIED_STATUSES,
        status_changed_at__lt=now - (timedelta(seconds=settings.PROJECT_FILE_UPLOAD_URL_TTL_SECONDS) + margin),
    )

    return list(
        FileVersion.objects.filter(object_deleted_at__isnull=True)
        .filter(abandoned | stale_attempt)
        .select_related("file", "file__project")
        .order_by("created_at", "id")[:limit]
    )


@transaction.atomic
def sweep_version(version, *, storage):
    """Delete one unverified object and end its attempt; return ``(swept, released)``.

    ``swept`` is False when the object could not be deleted: the row is then left
    exactly as it was, so the next run retries it, and nothing about it claims to
    have been cleaned up. Otherwise the object is gone by its **exact stored key**
    and the row carries the terminal marker, which is what keeps it out of every
    later selection.

    ``released`` is the guarded release's own verdict, and that is the only thing
    that may move a counter: a row an abort or a failed finalize already released
    matches zero rows here and reports False, so the single-fire guarantee holds
    across every actor (ARCH-001 §2.8 item 3, N-02c).

    Taking the attempt over - the terminal ``failed`` status, the object-deleted
    marker and the guarded release - is one database transaction, and it runs only
    after the object is gone: a later finalize therefore cannot match the settle
    predicate (``status='uploading'``) and cannot settle a row whose bytes were
    removed.
    """
    # Match finalize/purge lock ordering and revalidate before deleting bytes.
    quota_row, usage_row = quota_module.lock_usage_rows(version.file.project)
    version = FileVersion.objects.select_for_update().filter(pk=version.pk).first()
    if version is None or version.status not in UNVERIFIED_STATUSES:
        return False, False

    try:
        deleted = storage.delete_files([version.object_key])
    except Exception as exc:
        # ``delete_files`` reports a failed API call as False; a client can also
        # raise outside botocore's ClientError (a connection error, a timeout). Both
        # mean "this object is still stored", so both leave the row for the next run.
        log_exception(exc)
        deleted = False

    if not deleted:
        return False, False

    with transaction.atomic():
        version.mark_status(FileVersion.Status.FAILED, save=False)
        version.object_deleted_at = timezone.now()
        # A version with no stored object must never keep the active pointer: the
        # same rule the purge applies, so no reader can serve a key that is gone.
        version.is_active = False
        version.save(
            update_fields=["status", "status_changed_at", "object_deleted_at", "is_active", "updated_at"]
        )

        released = quota_module.release(quota_row, usage_row, version)

    # Counted after the marker commits, so the counter and the row that justifies it
    # agree: this is the deletion mechanism for objects with no verified version, and
    # the number is what says the sweep is doing work (AC-20, AC-31).
    observability.increment(
        observability.SWEEP_DELETIONS,
        workspace_id=version.file.project.workspace_id,
        project_id=version.file.project_id,
        file_id=version.file_id,
        version_no=version.version_no,
        object_key=version.object_key,
        reservation_released=released,
    )

    return True, released


def cleanup_unverified_batch(*, limit=SWEEP_BATCH_SIZE):
    """Sweep up to ``limit`` unverified objects; return the run's counters.

    One row's failure must not abort the batch - the row keeps its object and the
    next run retries it, while every other row is still swept this run.
    """
    storage = S3Storage()
    swept = 0
    released = 0
    failed = 0

    for version in unverified_versions(limit=limit):
        try:
            row_swept, row_released = sweep_version(version, storage=storage)
        except Exception as exc:
            log_exception(exc)
            row_swept, row_released = False, False

        if row_swept:
            swept += 1
        else:
            failed += 1
        if row_released:
            released += 1

    return {
        "swept": swept,
        "failed": failed,
        "reservations_released": released,
        "scanned": swept + failed,
    }


def resurrection_candidates(*, limit=RECHECK_BATCH_SIZE):
    """Rows the sweep marked over ``RESURRECTION_WINDOW`` ago and that are still non-active.

    The status filter is the second half of the guard: a row that has since been
    retried, activated or superseded holds a verified object again (or a fresh
    attempt that the sweep owns by the ordinary predicate), so re-checking it would
    be re-checking something that is no longer the sweep's business. A purged file is
    excluded as §4.4 requires; the value names the terminal audit action rather than
    a stored state (a purged file's row is gone, and its versions with it), so in
    practice the file row's absence is what carries that case.
    """
    cutoff = timezone.now() - RESURRECTION_WINDOW

    return list(
        FileVersion.objects.filter(
            object_deleted_at__isnull=False,
            object_deleted_at__lt=cutoff,
            status__in=UNVERIFIED_STATUSES,
        )
        .exclude(file__status=FileObject.Status.PURGED)
        .select_related("file")
        .order_by("object_deleted_at", "id")[:limit]
    )


def recheck_version(version, *, storage):
    """HEAD this row's exact key and delete it again if a late PUT recreated it.

    Returns ``"deleted"`` (an object was there and is now gone again - the
    resurrection the guard exists for), ``"absent"`` (nothing is stored under the
    key: the ordinary case, and no call is made to delete anything) or ``"failed"``
    (the object is there and could not be deleted, so the next run retries it).
    """
    if storage.get_object_metadata(version.object_key) is None:
        return "absent"

    try:
        deleted = storage.delete_files([version.object_key])
    except Exception as exc:
        log_exception(exc)
        deleted = False

    return "deleted" if deleted else "failed"


def recheck_deleted_batch(*, limit=RECHECK_BATCH_SIZE):
    """Re-check the rows the sweep marked; return the run's counters.

    ``resurrection_detected`` is the operational signal §4.4 asks for: a non-zero
    value means late PUTs are landing after the sweep and the margin needs widening.
    """
    storage = S3Storage()
    checked = 0
    deleted = 0
    failed = 0

    for version in resurrection_candidates(limit=limit):
        checked += 1
        try:
            outcome = recheck_version(version, storage=storage)
        except Exception as exc:
            log_exception(exc)
            outcome = "failed"

        if outcome == "deleted":
            deleted += 1
        elif outcome == "failed":
            failed += 1

    return {"resurrection_detected": deleted, "failed": failed, "checked": checked}
