# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import uuid

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from plane.api.views.base import BaseAPIView
from plane.app.permissions import ProjectPagePermission
from plane.bgtasks.storage_metadata_task import get_asset_object_metadata
from plane.db.models import FileAsset, Page, Project
from plane.settings.storage import S3Storage
from plane.utils.path_validator import sanitize_filename


PAGE_IMAGE_MIME_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}


class PageAssetMixin:
    permission_classes = [ProjectPagePermission]

    def get_page(self):
        return Page.objects.get(
            id=self.kwargs["page_id"],
            workspace__slug=self.kwargs["slug"],
            project_pages__project_id=self.kwargs["project_id"],
            project_pages__deleted_at__isnull=True,
        )

    def get_project(self):
        return Project.objects.get(
            id=self.kwargs["project_id"],
            workspace__slug=self.kwargs["slug"],
            archived_at__isnull=True,
        )

    def get_assets(self):
        return FileAsset.objects.filter(
            workspace__slug=self.kwargs["slug"],
            project_id=self.kwargs["project_id"],
            page_id=self.kwargs["page_id"],
            entity_type=FileAsset.EntityTypeContext.PAGE_DESCRIPTION,
            is_deleted=False,
        )

    def serialize_asset(self, asset, request, include_download_url=False):
        data = {
            "id": str(asset.id),
            "asset_id": str(asset.id),
            "asset_url": asset.asset_url,
            "name": asset.attributes.get("name", ""),
            "type": asset.attributes.get("type", ""),
            "size": asset.attributes.get("size", asset.size),
            "is_uploaded": asset.is_uploaded,
            "created_at": asset.created_at,
            "updated_at": asset.updated_at,
        }
        if include_download_url and asset.is_uploaded:
            storage = S3Storage(request=request)
            data["download_url"] = storage.generate_presigned_url(
                object_name=asset.asset.name,
                disposition="inline",
                filename=asset.attributes.get("name"),
            )
        return data


class ProjectPageAssetListCreateAPIEndpoint(PageAssetMixin, BaseAPIView):
    def get(self, request, slug, project_id, page_id):
        self.get_page()
        return self.paginate(
            request=request,
            queryset=self.get_assets().order_by("-created_at"),
            on_results=lambda assets: [self.serialize_asset(asset, request) for asset in assets],
        )

    def post(self, request, slug, project_id, page_id):
        page = self.get_page()
        project = self.get_project()
        name = sanitize_filename(request.data.get("name"))
        content_type = (request.data.get("type") or "").split(";", 1)[0].strip().lower()

        try:
            size = int(request.data.get("size", 0))
        except (TypeError, ValueError):
            size = 0

        if not name or size <= 0:
            return Response(
                {"error": "name and a positive size are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if size > settings.FILE_SIZE_LIMIT:
            return Response(
                {"error": f"File exceeds the {settings.FILE_SIZE_LIMIT}-byte upload limit"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if content_type not in PAGE_IMAGE_MIME_TYPES:
            return Response(
                {"error": "Only PNG, JPEG, GIF, and WebP page images are allowed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        asset_key = f"{project.workspace_id}/{uuid.uuid4().hex}-{name}"
        asset = FileAsset.objects.create(
            attributes={"name": name, "type": content_type, "size": size},
            asset=asset_key,
            size=size,
            workspace=project.workspace,
            project=project,
            page=page,
            created_by=request.user,
            entity_type=FileAsset.EntityTypeContext.PAGE_DESCRIPTION,
        )
        upload_data = S3Storage(request=request).generate_presigned_post(
            object_name=asset_key,
            file_type=content_type,
            file_size=size,
        )
        if not upload_data:
            asset.delete()
            return Response(
                {"error": "Could not create an upload URL"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                **self.serialize_asset(asset, request),
                "upload_data": upload_data,
            },
            status=status.HTTP_201_CREATED,
        )


class ProjectPageAssetDetailAPIEndpoint(PageAssetMixin, BaseAPIView):
    def get_asset(self):
        return self.get_assets().get(id=self.kwargs["asset_id"])

    def get(self, request, slug, project_id, page_id, asset_id):
        self.get_page()
        return Response(
            self.serialize_asset(self.get_asset(), request, include_download_url=True),
            status=status.HTTP_200_OK,
        )

    def patch(self, request, slug, project_id, page_id, asset_id):
        self.get_page()
        asset = self.get_asset()
        asset.is_uploaded = bool(request.data.get("is_uploaded", True))
        asset.save(update_fields=["is_uploaded", "updated_at"])
        if asset.is_uploaded and not asset.storage_metadata:
            get_asset_object_metadata.delay(asset_id=str(asset.id))
        return Response(self.serialize_asset(asset, request), status=status.HTTP_200_OK)

    def delete(self, request, slug, project_id, page_id, asset_id):
        self.get_page()
        asset = self.get_asset()
        asset.is_deleted = True
        asset.deleted_at = timezone.now()
        asset.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
