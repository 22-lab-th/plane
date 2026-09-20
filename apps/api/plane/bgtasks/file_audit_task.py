# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Mask the audit trail's personal-data columns once they pass their window (R-NFR-13).

The audit *skeleton* is kept for the project's lifetime: actor id, action, target
(``file_id``, ``project``, ``version_no``) and timestamps are never touched. What this
job clears is the personal data the row was only allowed to keep for a bounded time -
``ip_address``, ``user_agent``, ``actor_display`` and ``file_name_snapshot`` - so an
erasure request (R-LEG-2) is satisfied by masking rather than by deleting history
(AD-15).

It is idempotent without needing a marker column: the selection is "older than the
window **and** still holding personal data", so a masked row is never selected again
and no row is rewritten twice. The counts it returns are operational - the user-facing
trail records user actions, not maintenance runs (the same reason the quota
reconciliation's drift numbers stay out of it).

**Known residual exposure, left deliberately untouched.** R-NFR-13 names exactly four
columns, and this job masks exactly those. ``metadata`` is not among them, yet rows
carry three things inside it that the four columns would otherwise have covered:

* a rename row stores ``previous_name`` and a link row stores the human
  ``entity_identifier`` - names, so this is a residual personal-data exposure inside the
  window, and narrowing it is a change to the retention contract (a requirements
  revision under R-LEG-2 / R-NFR-13), not a task-level decision;
* a purged row stores ``object_keys``, and those are **kept on purpose**: a key alone
  grants nothing (a URL is what would), and the keys are the forensic record R-LEG-2
  needs to show that every version object of an erased file was covered. Replacing them
  with a digest would destroy exactly the evidence the erasure claim rests on.

Until a requirements revision says otherwise, this docstring is the record of all three.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

# Third party imports
from celery import shared_task

# Module imports
from plane.db.models import FileAccessLog

#: Rows one statement masks, so a long-neglected trail is drained in batches rather
#: than in one unbounded write.
MASK_BATCH_SIZE = 1000


def mask_expired_audit_pii(batch_size=MASK_BATCH_SIZE):
    """Mask every row past ``AUDIT_PII_RETENTION_DAYS``; return what was masked."""
    cutoff = timezone.now() - timedelta(days=settings.AUDIT_PII_RETENTION_DAYS)
    still_personal = (
        Q(ip_address__isnull=False) | ~Q(user_agent="") | ~Q(actor_display="") | ~Q(file_name_snapshot="")
    )

    masked = 0
    while True:
        batch = list(
            FileAccessLog.objects.filter(created_at__lt=cutoff)
            .filter(still_personal)
            .order_by("created_at")
            .values_list("id", flat=True)[:batch_size]
        )
        if not batch:
            break

        masked += FileAccessLog.objects.filter(id__in=batch).update(
            ip_address=None,
            user_agent="",
            actor_display="",
            file_name_snapshot="",
        )

        if len(batch) < batch_size:
            break

    return {
        "masked_rows": masked,
        "retention_days": settings.AUDIT_PII_RETENTION_DAYS,
        "cutoff": cutoff.isoformat(),
    }


@shared_task
def mask_audit_pii(batch_size=MASK_BATCH_SIZE):
    """Scheduled entry point (daily). See :func:`mask_expired_audit_pii`."""
    return mask_expired_audit_pii(batch_size=batch_size)
