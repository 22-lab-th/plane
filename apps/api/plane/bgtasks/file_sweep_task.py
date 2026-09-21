# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scheduled halves of the unverified-object sweep (AC-20, ARCH-001 §4.4).

Both tasks are thin wrappers: the predicate, the deletion, the guarded release and
the counters live in :mod:`plane.utils.file_storage.sweep`, because a test drives
exactly the same functions the schedule does and a queue is never required for a
correct answer. The sweep runs hourly and the resurrection guard daily; both are
batch-bounded and idempotent, and neither lists the bucket for this purpose (AD-13).
"""

# Third party imports
from celery import shared_task

# Module imports
from plane.utils.file_storage.sweep import (
    RECHECK_BATCH_SIZE,
    SWEEP_BATCH_SIZE,
    cleanup_unverified_batch,
    recheck_deleted_batch,
)


@shared_task
def cleanup_unverified_objects(batch_size=SWEEP_BATCH_SIZE):
    """Delete objects with no verified version and release what they reserved.

    Returns ``{"swept": n, "failed": n, "reservations_released": n, "scanned": n}``.
    ``failed`` counts rows whose object deletion failed: those rows are untouched and
    the next run retries them, so a failure is reported and never silently dropped
    (the shape the purge uses for the same reason, R3-02).
    """
    return cleanup_unverified_batch(limit=batch_size)


@shared_task
def recheck_deleted_objects(batch_size=RECHECK_BATCH_SIZE):
    """Re-HEAD the keys the sweep deleted and remove any a late PUT recreated.

    Returns ``{"resurrection_detected": n, "failed": n, "checked": n}``. A non-zero
    ``resurrection_detected`` is the signal that the sweep's margin is too narrow.
    """
    return recheck_deleted_batch(limit=batch_size)


@shared_task
def cleanup_failed_copies(batch_size=100):
    """Retry durable copy attempts, including writes that outlive a timeout."""
    from plane.utils.file_storage.copy_cleanup import cleanup_copy_keys

    return cleanup_copy_keys(limit=batch_size)
