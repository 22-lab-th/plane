# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Retry deletion of attempted copies without losing timed-out object keys."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from plane.db.models import FileCopyCleanup, FileVersion
from plane.license.utils.instance_value import get_storage_configuration
from plane.settings.storage import S3Storage
from plane.utils.exception_logger import log_exception


def cleanup_copy_keys(*, ids=None, limit=100):
    now = timezone.now()
    candidates = FileCopyCleanup.objects.order_by("next_cleanup_at", "id")
    if ids is None:
        candidates = candidates.filter(next_cleanup_at__lte=now)
    else:
        candidates = candidates.filter(pk__in=ids)
    candidate_ids = list(candidates.values_list("pk", flat=True)[:limit])
    configuration = get_storage_configuration()
    storage = S3Storage()
    result = {"deleted": 0, "failed": 0, "protected": 0}
    for row_id in candidate_ids:
        with transaction.atomic():
            row = FileCopyCleanup.objects.select_for_update(skip_locked=True).filter(pk=row_id).first()
            if row is None or (ids is None and row.next_cleanup_at > now):
                continue
            if FileVersion.all_objects.filter(object_key=row.object_key).exists():
                # Never delete a key transferred to a file, even if a journal was
                # retained by an older release or by a manual recovery.
                row.delete()
                result["protected"] += 1
                continue
            identity_matches = (
                row.bucket == configuration["bucket_name"]
                and row.provider == configuration["provider"]
                and row.endpoint_url == (configuration["endpoint_url"] or "")
            )
            deleted = False
            if identity_matches:
                try:
                    deleted = storage.delete_files([row.object_key])
                except Exception as exc:
                    log_exception(exc)
            if deleted:
                row.last_deleted_at = now
                result["deleted"] += 1
            else:
                result["failed"] += 1
            # Retain tombstones: a timed-out CopyObject may materialise after a
            # successful delete. Recheck without listing the bucket or storing PII.
            row.next_cleanup_at = now + (timedelta(days=1) if deleted else timedelta(hours=1))
            row.save(update_fields=["last_deleted_at", "next_cleanup_at"])
    return result
