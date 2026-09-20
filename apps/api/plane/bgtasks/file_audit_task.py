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
columns, and this job masks exactly those. ``metadata`` is not among them, yet some rows
carry a name inside it - a rename row stores ``previous_name``, and a link row stores
the human ``entity_identifier`` - so that text survives the masking window even though
the same information in ``file_name_snapshot`` does not. That inconsistency is a
residual personal-data exposure inside the window, not an oversight: narrowing it is a
change to the retention contract, so it belongs to a requirements revision (R-LEG-2 /
R-NFR-13) rather than to this task. Until then this docstring is the record of it.
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
