# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Scheduled purge of project files whose retention window has passed (R-DEL-4).

The task is a thin wrapper: the selection and the ordering live in
:mod:`plane.utils.file_storage.purge` because the ADMIN endpoint performs the very
same purge. It runs daily, is idempotent (the selection is a status query) and is
batch-bound, so a backlog is drained over several runs instead of holding one
worker and its storage calls open.

No object store lifecycle rule is involved (AD-12): every key this deletes comes
from ``file_versions``, and the bucket is never listed (AD-13).
"""

# Third party imports
from celery import shared_task

# Module imports
from plane.utils.file_storage.purge import PURGE_BATCH_SIZE, purge_expired_batch


@shared_task
def purge_expired_files(batch_size=PURGE_BATCH_SIZE):
    """Purge the files the schedule owns; return ``{"purged": n, "failed": n, "scanned": n}``.

    ``failed`` counts rows whose object deletion failed: those rows are now
    ``purge_failed`` and the next run retries them, so a failure is reported and
    never silently dropped (R3-02).
    """
    return purge_expired_batch(limit=batch_size)
