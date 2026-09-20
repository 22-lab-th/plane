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
from django.utils import timezone

# Module imports
from plane.db.models import FileVersion, ProjectStorageUsage, StorageQuota
from plane.utils.file_storage.errors import ProjectFileError


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
    """
    if requested_bytes < 0 or released_bytes < 0:
        raise ValueError("byte counts must not be negative")

    if quota.enforce and quota.limit_bytes is not None:
        projected = quota.used_bytes + quota.reserved_bytes + requested_bytes - released_bytes
        if projected > quota.limit_bytes:
            raise QuotaExceeded(
                "Workspace storage quota exceeded.",
                limit_bytes=quota.limit_bytes,
                projected_bytes=projected,
                level="workspace",
            )

    if usage.limit_bytes is not None:
        projected = usage.used_bytes + usage.reserved_bytes + requested_bytes - released_bytes
        if projected > usage.limit_bytes:
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
