# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The trash retention window: one definition, two consumers (R-LIFE-1, R-LEG-1, R-DEL-2).

The window is per project - ``Project.retention_days``, the PDPA s. 23(3)/s. 39 record
ARCH-001 §2.1 stores - and falls back to the deployment-wide ``PROJECT_FILE_TRASH_DAYS``
(owner-set default 30) for a project that does not set one. Nothing else may compute it,
because the two consumers have to agree or the feature contradicts itself:

* ``purge_expired_files`` selects a trashed file once ``deleted_at + window`` has passed
  (``deleted_at <= now - window``), and
* ``restore`` refuses a file the purge owns - the same instant - so a file is restorable
  exactly while it is not yet purgeable (R-DEL-2's "restore after retention expiry is
  refused" edge case, R-LIFE-1's "purge wins after the window").

A stored ``0`` is read as "not set" here, which is why the project API refuses to store
one (``ProjectSerializer``): a project that asked for a zero-day window and silently got
the 30-day default would be a retention claim the system does not honour.
"""

# Python imports
from datetime import timedelta

# Django imports
from django.conf import settings
from django.utils import timezone

#: The smallest retention period the API accepts. A window in fractional days is not
#: representable (the column is an integer), and the purge runs daily, so a value below
#: one day would only mean "purge on the next run" - a purge, not a retention period.
MIN_RETENTION_DAYS = 1


def window_days(raw_retention_days):
    """Return the effective window in days for a stored ``Project.retention_days`` value.

    Takes the raw column value rather than a project, so a caller that has already read
    the column (the purge selection groups projects by it) does not re-query.
    """
    return raw_retention_days or settings.PROJECT_FILE_TRASH_DAYS


def effective_retention_days(project):
    """Return the window that applies to one project."""
    return window_days(project.retention_days)


def retention_expires_at(file_object, *, days=None):
    """Return when a trashed file's window elapses, or ``None`` when it is not trashed.

    ``deleted_at`` is the trash marker and is set with ``status='trashed'`` in one
    update, so a row with no marker is not in the window at all.
    """
    if file_object.deleted_at is None:
        return None

    if days is None:
        days = window_days(file_object.project.retention_days)

    return file_object.deleted_at + timedelta(days=days)


def retention_has_expired(file_object, *, now=None):
    """Return ``True`` when the retention window has elapsed and the purge owns the row."""
    expires_at = retention_expires_at(file_object)
    if expires_at is None:
        return False

    return (now or timezone.now()) >= expires_at
