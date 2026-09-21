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
and no row is rewritten twice. The predicate travels with the ``UPDATE`` as well, so
two runs that overlap mask each row once and report it once.

**The run answers for itself.** One ``pii_masked`` event is appended per project whose
rows changed, carrying the counts and the window that produced them, so "which run
masked this row, and under which setting?" is answerable from the trail alone. The
event is written by the same recorder as every other row, with no request, because a
scheduled run has no actor, address or user agent to record; that is also why the event
holds none of the four masked columns. Nothing has to exempt it from the next run's
predicate (it has no personal data left to match), and it cannot recurse into a chain
of events for the same reason. A run that masks nothing appends nothing: the trail
records changes, not heartbeats, and a no-op second run leaves every row - including
the trail itself - byte-identical.

Two limits are stated rather than hidden. The events are appended *after* the row
changes commit (the batching above exists precisely so no single unbounded write is
held open), so a run that dies in between leaves masked rows whose event never arrives,
and the retry - which finds nothing left to mask - will not write it either. And a
masked row whose project row is truly gone (hard deleted, so ``project_id`` is null)
has no project to attach an event to: ``unrecorded_rows`` in the run's result and
``masked_rows`` are the only record of those. A **soft**-deleted project - what Plane's
ordinary delete-project action leaves behind - still gets its event, because the row
and the id are both still there.

**The window is configuration, not a toggle.** ``AUDIT_PII_RETENTION_DAYS`` goes
through ``plane.settings.common._retention_days``: unset, unparseable or negative
falls back to the documented default of 90 days, while ``0`` is a real value and means
"mask everything older than now", i.e. every row the next run sees. The trail keeps its
skeleton either way. No value disables the job - a very large one postpones every
masking to a date that never comes, which is a deliberate configuration rather than an
off switch - and a window past the end of the calendar is clamped to the oldest
representable instant for the same reason, instead of overflowing inside a scheduled
task.

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
from datetime import datetime, timedelta, timezone as dt_timezone

# Django imports
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

# Third party imports
from celery import shared_task

# Module imports
from plane.db.models import FileAccessLog, Project
from plane.utils.file_storage.audit import record_file_access

#: Rows one statement masks, so a long-neglected trail is drained in batches rather
#: than in one unbounded write.
MASK_BATCH_SIZE = 1000


def mask_expired_audit_pii(batch_size=MASK_BATCH_SIZE):
    """Mask every row past ``AUDIT_PII_RETENTION_DAYS``; return what was masked.

    Every count this returns, and every number an event carries, comes from an
    ``UPDATE`` rowcount - what this run changed - never from what it selected: a row
    another run got to first is counted by that run alone.
    """
    retention_days = settings.AUDIT_PII_RETENTION_DAYS
    # A window longer than the calendar is still not an error: it means no row can be
    # old enough. ``now - timedelta(days=…)`` underflows below ``datetime.min`` at
    # roughly 739 000 days, so the cutoff becomes the oldest representable instant and
    # the selection is empty - a nonsense configuration degrades into "nothing
    # qualifies" instead of raising from a scheduled task.
    try:
        cutoff = timezone.now() - timedelta(days=retention_days)
    except OverflowError:
        cutoff = datetime.min.replace(tzinfo=dt_timezone.utc)
    still_personal = (
        Q(ip_address__isnull=False) | ~Q(user_agent="") | ~Q(actor_display="") | ~Q(file_name_snapshot="")
    )

    masked = 0
    #: Rows this run changed per project, so each event carries that project's own
    #: number rather than the batch's.
    masked_per_project = {}

    while True:
        batch = list(
            FileAccessLog.objects.filter(created_at__lt=cutoff)
            .filter(still_personal)
            .order_by("created_at")
            .values_list("id", "project_id")[:batch_size]
        )
        if not batch:
            break

        # One statement per project in the batch. The predicate travels with the
        # ``UPDATE`` and each statement counts the rows *it* changed, so an
        # overlapping run that cleared part of this batch in between cannot make this
        # one report phantom work - neither in the total nor in a project's event.
        ids_per_project = {}
        for row_id, project_id in batch:
            ids_per_project.setdefault(project_id, []).append(row_id)

        for project_id, row_ids in ids_per_project.items():
            changed = (
                FileAccessLog.objects.filter(id__in=row_ids)
                .filter(still_personal)
                .update(
                    ip_address=None,
                    user_agent="",
                    actor_display="",
                    file_name_snapshot="",
                )
            )
            masked += changed
            if changed:
                masked_per_project[project_id] = masked_per_project.get(project_id, 0) + changed

        if len(batch) < batch_size:
            break

    unrecorded = (
        _record_masking_run(masked_per_project, retention_days=retention_days, cutoff=cutoff) if masked else 0
    )

    return {
        "masked_rows": masked,
        # Masked rows this run could not attach an event to (their project row is
        # gone): reported rather than dropped, so the gap is visible in the result.
        "unrecorded_rows": unrecorded,
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
    }


@shared_task
def mask_audit_pii(batch_size=MASK_BATCH_SIZE):
    """Scheduled entry point (daily). See :func:`mask_expired_audit_pii`."""
    return mask_expired_audit_pii(batch_size=batch_size)


def _record_masking_run(masked_per_project, *, retention_days, cutoff):
    """Append one ``pii_masked`` event per project whose rows this run changed.

    Written with no request on purpose: the event has no actor, address or user agent
    to record, and so nothing on it can be masked, matched by the next selection, or
    turned into another event. Returns the number of masked rows it could not
    attribute, which is nonzero only when the project row itself is gone.
    """
    # ``all_objects``: a soft-deleted project (Plane's ordinary delete-project action
    # leaves the row and sets ``deleted_at``) still owns its audit rows, and a run
    # that masked them has to be recorded against it - the trail must not lose the
    # most common deletion path.
    projects = {
        project.id: project
        for project in Project.all_objects.filter(
            id__in=[pid for pid in masked_per_project if pid is not None]
        )
    }

    unrecorded = 0
    for project_id in sorted(masked_per_project, key=str):
        project = projects.get(project_id)
        if project is None:
            # The project row is truly gone (hard deleted, ``SET_NULL``): there is
            # nothing to attach the event to, and masking the orphaned rows waited for
            # no such thing. The run reports the count it could not attribute.
            unrecorded += masked_per_project[project_id]
            continue

        record_file_access(
            None,
            action=FileAccessLog.Action.PII_MASKED,
            project=project,
            metadata={
                "masked_rows": masked_per_project[project_id],
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
            },
        )

    return unrecorded
