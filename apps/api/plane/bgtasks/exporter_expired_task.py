# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import boto3
from datetime import timedelta

# Django imports
from django.utils import timezone
from django.db.models import Q

# Third party imports
from celery import shared_task
from botocore.client import Config

# Module imports
from plane.db.models import ExporterHistory
from plane.license.utils.instance_value import get_storage_configuration


@shared_task
def delete_old_s3_link():
    # Get a list of keys and IDs to process
    expired_exporter_history = ExporterHistory.objects.filter(
        Q(url__isnull=False) & Q(created_at__lte=timezone.now() - timedelta(days=8))
    ).values_list("key", "id")
    storage = get_storage_configuration()
    s3 = boto3.client(
        "s3",
        endpoint_url=storage["endpoint_url"],
        region_name=storage["region_name"],
        aws_access_key_id=storage["access_key_id"],
        aws_secret_access_key=storage["secret_access_key"],
        config=Config(
            signature_version=storage["signature_version"],
            s3={"addressing_style": storage["addressing_style"]},
        ),
    )

    for file_name, exporter_id in expired_exporter_history:
        # Delete object from S3
        if file_name:
            s3.delete_object(Bucket=storage["bucket_name"], Key=file_name)

        ExporterHistory.objects.filter(id=exporter_id).update(url=None)
