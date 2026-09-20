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
from plane.utils.file_storage.errors import FileUploadError


class QuotaExceeded(FileUploadError):
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
    """Return both counter rows for ``project``, creating missing ones (N-02a).

    The workspace row carries the ceiling counters because the limit is
    workspace-level; the project row carries the per-project counters and an
    optional per-project limit.
    """
    quota, _ = StorageQuota.objects.get_or_create(
        workspace_id=project.workspace_id,
        defaults={"limit_bytes": settings.PROJECT_FILE_WORKSPACE_QUOTA_BYTES},
    )
    usage, _ = ProjectStorageUsage.objects.get_or_create(project_id=project.id)
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
    rows and leaves the counters untouched (ARCH-001 §2.8 item 3).
    """
    if version.reservation_released_at is not None:
        return False

    released = FileVersion.objects.filter(pk=version.pk, reservation_released_at__isnull=True).update(
        reservation_released_at=timezone.now()
    )
    if not released:
        return False

    reserved_bytes = version.reserved_bytes or 0
    if reserved_bytes:
        _bump_counters(quota, usage, reserved_bytes=-reserved_bytes)

    return True


def _bump_counters(quota, usage, *, used_bytes=0, reserved_bytes=0):
    """Apply counter deltas to both rows and keep the in-memory copies in sync."""
    StorageQuota.objects.filter(pk=quota.pk).update(
        used_bytes=quota.used_bytes + used_bytes,
        reserved_bytes=quota.reserved_bytes + reserved_bytes,
    )
    ProjectStorageUsage.objects.filter(pk=usage.pk).update(
        used_bytes=usage.used_bytes + used_bytes,
        reserved_bytes=usage.reserved_bytes + reserved_bytes,
    )

    quota.used_bytes += used_bytes
    quota.reserved_bytes += reserved_bytes
    usage.used_bytes += used_bytes
    usage.reserved_bytes += reserved_bytes
