# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Quota reservation, settlement and release for project files (ARCH-001 §2.8).

Three rules shape this module:

* **Materialise before locking.** A ``SELECT ... FOR UPDATE`` on a row that does
  not exist takes no lock, so two racers would both pass the ceiling check. Both
  counter rows are therefore created with ``get_or_create`` before any lock is
  taken (N-02a).
* **One lock order.** The workspace ``storage_quotas`` row is always locked
  first and the project ``project_storage_usage`` row second, so two projects in
  one workspace can never deadlock and the workspace ceiling has a single
  serialisation point (N-02b).
* **Single-fire release.** Ending an attempt is a conditional update on
  ``reservation_released_at IS NULL``; only the caller whose statement matched a
  row may decrement the counters, so ``reserved_bytes`` can never go negative
  however many actors (abort, failed finalize, sweep) race for the release.

Every function that touches counters expects the caller to be inside the
transaction that holds the locks, except :func:`get_usage_rows` and
:func:`lock_usage_rows`, which own acquiring them.
"""

# Django imports
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

# Module imports
from plane.db.models import FileObject, FileVersion, Project, ProjectStorageUsage, StorageQuota
from plane.utils.file_storage import observability
from plane.utils.file_storage.errors import ProjectFileError

#: Version states whose bytes ``used_bytes`` accounts for (ARCH-001 §2.8 item 6).
#: A settled version counts until its *file* goes: ``purged`` and ``purge_failed``
#: rows stay counted while the file row exists, because a partial purge returns
#: before the counter transaction and only a whole-file purge gives the bytes back
#: once. ``uploading`` and ``failed`` versions were never settled - their bytes live
#: in ``reserved_bytes`` - and an unverified object is never counted as usable
#: storage, so they are excluded.
ACCOUNTED_VERSION_STATUSES = (
    FileVersion.Status.ACTIVE,
    FileVersion.Status.SUPERSEDED,
    FileVersion.Status.PURGED,
    FileVersion.Status.PURGE_FAILED,
)


class QuotaExceeded(ProjectFileError):
    """Raised when a reservation or a settlement would cross a configured ceiling."""

    code = "quota_exceeded"

    def __init__(self, message, *, limit_bytes=None, projected_bytes=None, level=None):
        super().__init__(
            message,
            limit_bytes=limit_bytes,
            projected_bytes=projected_bytes,
            level=level,
        )
        self.limit_bytes = limit_bytes
        self.projected_bytes = projected_bytes
        self.level = level


def get_usage_rows(project):
    """Return both counter rows for ``project``, creating or reviving them.

    Both rows are materialised before any lock is taken, because a
    ``SELECT ... FOR UPDATE`` on an absent row takes no lock and two racers would
    both pass the ceiling check (N-02a).

    The counters are durable infrastructure rather than user-visible objects, so
    a row that was soft-deleted is *revived* instead of replaced: the unique
    constraint spans soft-deleted rows, so inserting a second row would fail, and
    reusing the row also keeps its counts. The lookup therefore goes through the
    all-objects manager.
    """
    quota, _ = StorageQuota.all_objects.get_or_create(
        workspace_id=project.workspace_id,
        defaults={"limit_bytes": settings.PROJECT_FILE_WORKSPACE_QUOTA_BYTES},
    )
    if quota.deleted_at is not None:
        StorageQuota.all_objects.filter(pk=quota.pk).update(deleted_at=None)
        quota.deleted_at = None

    usage, _ = ProjectStorageUsage.all_objects.get_or_create(project_id=project.id)
    if usage.deleted_at is not None:
        ProjectStorageUsage.all_objects.filter(pk=usage.pk).update(deleted_at=None)
        usage.deleted_at = None

    return quota, usage


def lock_usage_rows(project):
    """Materialise and lock both counter rows: workspace first, then project."""
    quota, usage = get_usage_rows(project)
    quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)
    usage = ProjectStorageUsage.objects.select_for_update().get(pk=usage.pk)
    return quota, usage


def ensure_within_limits(quota, usage, *, requested_bytes, released_bytes=0):
    """Raise :class:`QuotaExceeded` unless ``requested_bytes`` still fits.

    :param released_bytes: capacity this attempt is giving back at the same time,
        which is how settlement re-checks against the server-observed size
        without double-counting the reservation it replaces.

    This is the one place a ceiling refuses a request, so it is also the one place
    the ``quota_rejections`` counter moves (AC-31): whichever door asked - the
    presign reservation, the finalize re-check against the server-observed size, or
    a copy - a refusal is counted once, with the ceiling that refused it.
    """
    if requested_bytes < 0 or released_bytes < 0:
        raise ValueError("byte counts must not be negative")

    if quota.enforce and quota.limit_bytes is not None:
        projected = quota.used_bytes + quota.reserved_bytes + requested_bytes - released_bytes
        if projected > quota.limit_bytes:
            observability.increment(
                observability.QUOTA_REJECTIONS,
                workspace_id=quota.workspace_id,
                project_id=usage.project_id,
                level="workspace",
                limit_bytes=quota.limit_bytes,
                projected_bytes=projected,
                requested_bytes=requested_bytes,
            )
            raise QuotaExceeded(
                "Workspace storage quota exceeded.",
                limit_bytes=quota.limit_bytes,
                projected_bytes=projected,
                level="workspace",
            )

    if usage.limit_bytes is not None:
        projected = usage.used_bytes + usage.reserved_bytes + requested_bytes - released_bytes
        if projected > usage.limit_bytes:
            observability.increment(
                observability.QUOTA_REJECTIONS,
                workspace_id=quota.workspace_id,
                project_id=usage.project_id,
                level="project",
                limit_bytes=usage.limit_bytes,
                projected_bytes=projected,
                requested_bytes=requested_bytes,
            )
            raise QuotaExceeded(
                "Project storage limit exceeded.",
                limit_bytes=usage.limit_bytes,
                projected_bytes=projected,
                level="project",
            )


def reserve(quota, usage, version, *, requested_bytes, expires_at):
    """Check the ceiling and reserve ``requested_bytes`` for ``version``.

    Called inside the presign transaction: the reservation and the row that
    carries it commit together, and the commit is what makes the presigned URL
    valid (ARCH-001 §2.8 item 1). Raises :class:`QuotaExceeded` before anything
    is written.
    """
    ensure_within_limits(quota, usage, requested_bytes=requested_bytes)

    version.reserved_bytes = requested_bytes
    version.reservation_expires_at = expires_at
    version.save(update_fields=["reserved_bytes", "reservation_expires_at", "updated_at"])

    _bump_counters(quota, usage, reserved_bytes=requested_bytes)
    return version


def settle(quota, usage, version, *, observed_bytes, activate, fields=None):
    """Settle a verified upload exactly once; return its status, or ``None``.

    The conditional update on ``status='uploading'`` is the idempotency gate: a
    repeated or concurrent finalize matches zero rows, returns ``None`` and
    touches no counters, so usage is settled once and one audit row is written
    (ARCH-001 §2.8 item 2, R-NFR-6). The reservation marker is set in the same
    statement, which is what stops any later actor from releasing it again.

    :param fields: extra column values to write in that same statement — the
        verification evidence (``etag``, ``storage_metadata``, …), so a settled
        version never exists without the evidence that justified it.
    """
    settled_status = FileVersion.Status.ACTIVE if activate else FileVersion.Status.SUPERSEDED
    settled_at = timezone.now()

    settled = FileVersion.objects.filter(pk=version.pk, status=FileVersion.Status.UPLOADING).update(
        status=settled_status,
        status_changed_at=settled_at,
        is_active=activate,
        size_bytes=observed_bytes,
        reservation_released_at=settled_at,
        # The reservation is consumed: the row must never keep holding counter
        # space after it was settled (T-118 carry-forward).
        reserved_bytes=0,
        **(fields or {}),
    )
    if not settled:
        return None

    reserved_bytes = version.reserved_bytes or 0
    try:
        ensure_within_limits(quota, usage, requested_bytes=observed_bytes, released_bytes=reserved_bytes)
    except QuotaExceeded:
        # The object is larger than the reservation allowed for. Fail the attempt
        # and hand its reservation to the ordinary guarded release, so the
        # decrement happens in exactly one place.
        FileVersion.objects.filter(pk=version.pk).update(
            status=FileVersion.Status.FAILED,
            status_changed_at=timezone.now(),
            is_active=False,
            reservation_released_at=None,
        )
        release(quota, usage, version)
        raise

    _bump_counters(quota, usage, used_bytes=observed_bytes, reserved_bytes=-reserved_bytes)
    return settled_status


def release(quota, usage, version):
    """Release ``version``'s reservation if no one has released it yet.

    Returns ``True`` only for the caller whose guarded statement matched a row;
    every later caller (abort retry, failed finalize, cleanup sweep) matches zero
    rows and leaves the counters untouched (ARCH-001 §2.8 item 3). The guarded
    statement also zeroes the row's ``reserved_bytes``, so a released row never
    keeps holding counter space, and the counters are clamped at zero so a
    release can never drive them negative.
    """
    if version.reservation_released_at is not None:
        return False

    released = FileVersion.objects.filter(pk=version.pk, reservation_released_at__isnull=True).update(
        reservation_released_at=timezone.now(),
        reserved_bytes=0,
    )
    if not released:
        return False

    # Read the amount this row held before the guarded statement zeroed it.
    reserved_bytes = version.reserved_bytes or 0
    if reserved_bytes:
        _bump_counters(quota, usage, reserved_bytes=-reserved_bytes, clamp_reserved=True)

    version.reserved_bytes = 0
    version.reservation_released_at = timezone.now()
    return True


def subtract_used(quota, usage, *, used_bytes):
    """Give ``used_bytes`` back to both counters after a purge.

    Not clamped at zero on purpose: a purge can only remove bytes that were
    counted (AD-09 keeps trashed files counted), so a negative result would be
    evidence of drift rather than something to hide (T-102 F-5).
    """
    if used_bytes:
        _bump_counters(quota, usage, used_bytes=-used_bytes)


def _bump_counters(quota, usage, *, used_bytes=0, reserved_bytes=0, clamp_reserved=False):
    """Apply counter deltas to both rows and keep the in-memory copies in sync."""
    quota_used = quota.used_bytes + used_bytes
    usage_used = usage.used_bytes + used_bytes
    quota_reserved = quota.reserved_bytes + reserved_bytes
    usage_reserved = usage.reserved_bytes + reserved_bytes

    if clamp_reserved:
        quota_reserved = max(quota_reserved, 0)
        usage_reserved = max(usage_reserved, 0)

    StorageQuota.objects.filter(pk=quota.pk).update(used_bytes=quota_used, reserved_bytes=quota_reserved)
    ProjectStorageUsage.objects.filter(pk=usage.pk).update(used_bytes=usage_used, reserved_bytes=usage_reserved)

    quota.used_bytes = quota_used
    quota.reserved_bytes = quota_reserved
    usage.used_bytes = usage_used
    usage.reserved_bytes = usage_reserved


def accounted_bytes(project):
    """Return the bytes this project's files account for (ARCH-001 §2.8 item 6).

    Summed from ``file_versions`` and from nothing else: the file row's
    ``object_key``/``current_version_no``/``size_bytes`` are display values (T-108
    F-2), and ``current_version_no == 0`` does not mean "nothing is stored". The
    join deliberately does not filter the *file* rows, so a trashed file (AD-09) and
    a file left ``purge_failed`` by a partial purge both keep their versions counted
    until the file row itself is gone.
    """
    total = FileVersion.objects.filter(
        file__project_id=project.id, status__in=ACCOUNTED_VERSION_STATUSES
    ).aggregate(total=Sum("size_bytes"))["total"]

    return int(total or 0)


def reserved_bytes(project):
    """Return the bytes this project's live reservations hold.

    Read from the version rows rather than from the counter: the row's
    ``reserved_bytes`` is the stored amount and ``reservation_released_at IS NULL``
    is the single-fire marker, so a recompute cannot invent a reservation and cannot
    forget one the counter lost.
    """
    total = FileVersion.objects.filter(
        file__project_id=project.id, reservation_released_at__isnull=True
    ).aggregate(total=Sum("reserved_bytes"))["total"]

    return int(total or 0)


def recompute_project_usage(project):
    """Rewrite this project's counters from the version rows; return the drift.

    Runs inside the shared lock pair (workspace first, then project), so a concurrent
    presign, finalize or purge cannot interleave with it. This is R-QUOTA-1's
    "self-correcting" guarantee: the version rows are the account of record and the
    counters are made to agree with them.

    ``file_count``/``version_count``/``recomputed_at`` are recorded for the readers
    that want a snapshot (T-120's observability, the UI's last-reconciled line); no
    decision anywhere reads them back.
    """
    with transaction.atomic():
        quota_row, usage_row = lock_usage_rows(project)

        expected = accounted_bytes(project)
        expected_reserved = reserved_bytes(project)
        files = FileObject.all_objects.filter(project_id=project.id).count()
        versions = FileVersion.objects.filter(file__project_id=project.id).count()

        drift = usage_row.used_bytes - expected
        reserved_drift = usage_row.reserved_bytes - expected_reserved

        ProjectStorageUsage.objects.filter(pk=usage_row.pk).update(
            used_bytes=expected,
            reserved_bytes=expected_reserved,
            file_count=files,
            version_count=versions,
            recomputed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        usage_row.used_bytes = expected
        usage_row.reserved_bytes = expected_reserved

    return {
        "project_id": str(project.id),
        "used_bytes": expected,
        "drift_bytes": drift,
        "reserved_drift_bytes": reserved_drift,
        "file_count": files,
        "version_count": versions,
    }


def recompute_workspace_usage(workspace_id):
    """Make the workspace counters the sum of its projects'; return the drift.

    The workspace row carries the ceiling counters (R-QUOTA-2), so it is the sum of
    the project rows rather than a number of its own: this is what keeps the ceiling
    honest after a project-level correction.
    """
    with transaction.atomic():
        quota_row = StorageQuota.objects.select_for_update().filter(workspace_id=workspace_id).first()
        if quota_row is None:
            return {"workspace_id": str(workspace_id), "used_bytes": 0, "drift_bytes": 0, "reserved_drift_bytes": 0}

        totals = ProjectStorageUsage.objects.filter(project__workspace_id=workspace_id).aggregate(
            used=Sum("used_bytes"), reserved=Sum("reserved_bytes")
        )
        expected = int(totals["used"] or 0)
        expected_reserved = int(totals["reserved"] or 0)
        drift = quota_row.used_bytes - expected
        reserved_drift = quota_row.reserved_bytes - expected_reserved

        StorageQuota.objects.filter(pk=quota_row.pk).update(
            used_bytes=expected,
            reserved_bytes=expected_reserved,
            updated_at=timezone.now(),
        )
        quota_row.used_bytes = expected
        quota_row.reserved_bytes = expected_reserved

    return {
        "workspace_id": str(workspace_id),
        "used_bytes": expected,
        "drift_bytes": drift,
        "reserved_drift_bytes": reserved_drift,
    }
