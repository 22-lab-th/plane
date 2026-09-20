# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Reconcile the storage counters with the version rows and the bucket (ARCH-001 §2.8 item 7).

Two comparisons, because neither alone is enough:

* **per project**, the stored ``used_bytes`` is compared with the sum over that
  project's version rows and corrected - this is R-QUOTA-1's "self-correcting"
  guarantee and it detects a lost or doubled writer (a failed purge, a race);
* **per account**, the bucket's own ``UsageSummary`` (a Class B call) is compared
  with the total the version rows account for, and divergence beyond
  ``PROJECT_FILE_QUOTA_TOLERANCE_BYTES × active_projects`` is recorded as an alert.
  ``UsageSummary`` is not per project (N-11), which is exactly why it is useful
  here: it sees bytes no row accounts for - the lying-client object, an object whose
  purge failed - that no per-project recompute could notice.

The task is a thin orchestrator over :mod:`plane.utils.file_storage.quota`: the same
recompute runs in tests by calling this function directly, and the queue is never
required for a correct answer.
"""

# Django imports
from django.conf import settings
from django.db.models import Sum

# Third party imports
from celery import shared_task

# Module imports
from plane.db.models import FileObject, Project, ProjectStorageUsage, StorageQuota, Workspace
from plane.settings.storage import S3Storage
from plane.utils.exception_logger import log_exception
from plane.utils.file_storage.quota import recompute_project_usage, recompute_workspace_usage

#: Projects one run reconciles, so a large deployment drains over several runs
#: instead of holding a worker for an unbounded time.
RECONCILE_BATCH_SIZE = 500


def reconcile_storage_batch(batch_size=RECONCILE_BATCH_SIZE):
    """Recompute every project's counters and compare the total with the bucket.

    Returns a summary rather than only logging it, so a caller (a test, an operator,
    a future alerting hook) can act on the numbers. ``alert`` is true only when the
    bucket reports more bytes than the version rows account for, beyond the
    tolerance; ``bucket_usage_available`` is false when the provider cannot answer,
    which is reported rather than read as zero.
    """
    corrected = []
    checked = 0

    for project in Project.objects.all().order_by("created_at")[:batch_size]:
        checked += 1
        try:
            result = recompute_project_usage(project)
        except Exception as exc:
            log_exception(exc)
            continue
        if result["drift_bytes"] or result["reserved_drift_bytes"]:
            corrected.append(result)

    # The workspace rows are the sum of their projects (R-QUOTA-2's ceiling lives there).
    for workspace in Workspace.objects.all().order_by("created_at")[:batch_size]:
        recompute_workspace_usage(workspace.id)

    accounted = int(ProjectStorageUsage.objects.aggregate(total=Sum("used_bytes"))["total"] or 0)
    #: Projects that hold at least one file row: the multiplier the tolerance is
    #: expressed in, so a large deployment is not alerted by its own size.
    active_projects = FileObject.all_objects.values("project_id").distinct().count()
    tolerance = settings.PROJECT_FILE_QUOTA_TOLERANCE_BYTES * max(active_projects, 1)

    bucket_bytes = S3Storage().get_bucket_usage_bytes()
    unaccounted = None if bucket_bytes is None else bucket_bytes - accounted
    alert = unaccounted is not None and unaccounted > tolerance

    if alert:
        log_exception(
            RuntimeError(
                "storage reconciliation: the bucket holds more than the version rows account for "
                f"(bucket={bucket_bytes}, accounted={accounted}, tolerance={tolerance})"
            ),
            warning=True,
        )

    return {
        "projects_checked": checked,
        "projects_corrected": len(corrected),
        "corrections": corrected,
        "accounted_bytes": accounted,
        "bucket_usage_available": bucket_bytes is not None,
        "bucket_bytes": bucket_bytes,
        "unaccounted_bytes": unaccounted,
        "tolerance_bytes": tolerance,
        "active_projects": active_projects,
        "alert": alert,
    }


@shared_task
def reconcile_storage_usage(batch_size=RECONCILE_BATCH_SIZE):
    """Scheduled entry point (daily). See :func:`reconcile_storage_batch`."""
    return reconcile_storage_batch(batch_size=batch_size)
