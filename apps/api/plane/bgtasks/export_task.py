# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import io
import zipfile
from typing import List
import boto3
from botocore.client import Config
from uuid import UUID

# Third party imports
from celery import shared_task

# Django imports
from django.conf import settings
from django.utils import timezone
from django.db.models import Prefetch

# Module imports
from plane.db.models import ExporterHistory, Issue, IssueComment, IssueRelation, IssueSubscriber
from plane.utils.exception_logger import log_exception
from plane.utils.porters.exporter import DataExporter
from plane.utils.porters.serializers.issue import IssueExportSerializer
from plane.license.utils.instance_value import get_storage_configuration


def create_zip_file(files: List[tuple[str, str | bytes]]) -> io.BytesIO:
    """
    Create a ZIP file from the provided files.
    """
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for filename, file_content in files:
            zipf.writestr(filename, file_content)

    zip_buffer.seek(0)
    return zip_buffer


# TODO: Change the upload_to_s3 function to use the new storage method with entry in file asset table
def upload_to_s3(zip_file: io.BytesIO, workspace_id: UUID, token_id: str, slug: str) -> None:
    """
    Upload a ZIP file to S3 and generate a presigned URL.
    """
    file_name = f"{workspace_id}/export-{slug}-{token_id[:6]}-{str(timezone.now().date())}.zip"
    expires_in = 7 * 24 * 60 * 60

    storage = get_storage_configuration()
    bucket_name = storage["bucket_name"]
    client_config = Config(
        signature_version=storage["signature_version"],
        s3={"addressing_style": storage["addressing_style"]},
    )

    if settings.USE_MINIO and storage["provider"] != "r2":
        upload_s3 = boto3.client(
            "s3",
            endpoint_url=storage["endpoint_url"],
            aws_access_key_id=storage["access_key_id"],
            aws_secret_access_key=storage["secret_access_key"],
            region_name=storage["region_name"],
            config=client_config,
        )
        upload_s3.upload_fileobj(
            zip_file,
            bucket_name,
            file_name,
            ExtraArgs={"ACL": "public-read", "ContentType": "application/zip"},
        )

        custom_domain = getattr(settings, "AWS_S3_CUSTOM_DOMAIN", None)
        public_endpoint_url = storage["endpoint_url"]
        if custom_domain:
            public_domain = str(custom_domain).replace(f"/{settings.AWS_STORAGE_BUCKET_NAME}", "")
            public_endpoint_url = f"{settings.AWS_S3_URL_PROTOCOL}//{public_domain}/"

        presign_s3 = boto3.client(
            "s3",
            endpoint_url=public_endpoint_url,
            aws_access_key_id=storage["access_key_id"],
            aws_secret_access_key=storage["secret_access_key"],
            region_name=storage["region_name"],
            config=client_config,
        )

        presigned_url = presign_s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": file_name},
            ExpiresIn=expires_in,
        )
    else:
        s3 = boto3.client(
            "s3",
            endpoint_url=storage["endpoint_url"],
            region_name=storage["region_name"],
            aws_access_key_id=storage["access_key_id"],
            aws_secret_access_key=storage["secret_access_key"],
            config=client_config,
        )

        s3.upload_fileobj(
            zip_file,
            bucket_name,
            file_name,
            ExtraArgs={"ContentType": "application/zip"},
        )

        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": file_name},
            ExpiresIn=expires_in,
        )

    exporter_instance = ExporterHistory.objects.get(token=token_id)

    # Update the exporter instance with the presigned url
    if presigned_url:
        exporter_instance.url = presigned_url
        exporter_instance.status = "completed"
        exporter_instance.key = file_name
    else:
        exporter_instance.status = "failed"

    exporter_instance.save(update_fields=["status", "url", "key"])


@shared_task
def issue_export_task(
    provider: str,
    workspace_id: UUID,
    project_ids: List[str],
    token_id: str,
    multiple: bool,
    slug: str,
):
    """
    Export issues from the workspace.
    provider (str): The provider to export the issues to csv | json | xlsx.
    token_id (str): The export object token id.
    multiple (bool): Whether to export the issues to multiple files per project.
    """
    try:
        exporter_instance = ExporterHistory.objects.get(token=token_id)
        exporter_instance.status = "processing"
        exporter_instance.save(update_fields=["status"])

        # Build base queryset for issues
        workspace_issues = (
            Issue.objects.filter(
                workspace__id=workspace_id,
                project_id__in=project_ids,
                project__project_projectmember__member=exporter_instance.initiated_by_id,
                project__project_projectmember__is_active=True,
                project__archived_at__isnull=True,
            )
            .select_related(
                "project",
                "workspace",
                "state",
                "created_by",
                "estimate_point",
            )
            .prefetch_related(
                "labels",
                "issue_cycle__cycle",
                "issue_module__module",
                "assignees",
                "issue_link",
                Prefetch(
                    "issue_subscribers",
                    queryset=IssueSubscriber.objects.select_related("subscriber"),
                ),
                Prefetch(
                    "issue_comments",
                    queryset=IssueComment.objects.select_related("actor").order_by("created_at"),
                ),
                Prefetch(
                    "issue_relation",
                    queryset=IssueRelation.objects.select_related("related_issue", "related_issue__project"),
                ),
                Prefetch(
                    "issue_related",
                    queryset=IssueRelation.objects.select_related("issue", "issue__project"),
                ),
                Prefetch(
                    "parent",
                    queryset=Issue.objects.select_related("type", "project"),
                ),
            )
        )

        # Create exporter for the specified format
        try:
            exporter = DataExporter(IssueExportSerializer, format_type=provider)
        except ValueError as e:
            # Invalid format type
            exporter_instance = ExporterHistory.objects.get(token=token_id)
            exporter_instance.status = "failed"
            exporter_instance.reason = str(e)
            exporter_instance.save(update_fields=["status", "reason"])
            return

        files = []
        if multiple:
            # Export each project separately with its own queryset
            for project_id in project_ids:
                project_issues = workspace_issues.filter(project_id=project_id)
                export_filename = f"{slug}-{project_id}"
                filename, content = exporter.export(export_filename, project_issues)
                files.append((filename, content))
        else:
            # Export all issues in a single file
            export_filename = f"{slug}-{workspace_id}"
            filename, content = exporter.export(export_filename, workspace_issues)
            files.append((filename, content))

        zip_buffer = create_zip_file(files)
        upload_to_s3(zip_buffer, workspace_id, token_id, slug)

    except Exception as e:
        exporter_instance = ExporterHistory.objects.get(token=token_id)
        exporter_instance.status = "failed"
        exporter_instance.reason = str(e)
        exporter_instance.save(update_fields=["status", "reason"])
        log_exception(e)
        return
