# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

import base64
import binascii
import os

import requests
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from plane.api.serializers.page import PublicPageSerializer
from plane.api.views.base import BaseAPIView
from plane.app.permissions import ProjectPagePermission, WorkspaceEntityPermission
from plane.app.views.page.base import (
    unarchive_archive_page_and_descendants,
    validate_page_parent,
    validate_sibling_folder_name,
)
from plane.bgtasks.page_transaction_task import page_transaction
from plane.db.models import Page, Project, ProjectMember, ProjectPage, Workspace


class PageQuerysetMixin:
    serializer_class = PublicPageSerializer
    model = Page
    use_read_replica = True

    def visible_pages(self):
        return (
            Page.objects.filter(workspace__slug=self.kwargs["slug"])
            .filter(Q(owned_by=self.request.user) | Q(access=Page.PUBLIC_ACCESS))
            .select_related("workspace", "owned_by", "parent")
            .prefetch_related("projects")
            .distinct()
        )

    def filter_page_tree(self, request, queryset):
        """Apply opt-in tree navigation without breaking legacy flat lists."""
        parent_param = request.query_params.get("parent")
        folders_only = request.query_params.get("folders_only", "false").lower() == "true"
        if folders_only:
            return queryset.filter(node_type=Page.FOLDER_NODE)
        if parent_param is None:
            return queryset
        if parent_param in ("", "null", "root"):
            return queryset.filter(parent__isnull=True)

        parent, parent_error = validate_page_parent(
            self.kwargs["slug"],
            self.kwargs.get("project_id"),
            parent_param,
            user=request.user,
        )
        if parent_error:
            return parent_error
        return queryset.filter(parent_id=parent.id)

    @staticmethod
    def hierarchy_order(queryset):
        return queryset.order_by(
            Case(
                When(node_type=Page.FOLDER_NODE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            "sort_order",
            "name",
            "id",
        )

    def validate_hierarchy_update(self, request, page):
        project_id = self.kwargs.get("project_id")
        if not project_id:
            return None, request.data

        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if "node_type" in data and data["node_type"] != page.node_type:
            return Response({"error": "node_type cannot be changed"}, status=status.HTTP_400_BAD_REQUEST), None
        if page.node_type == Page.FOLDER_NODE and any(
            key in data for key in ("description_html", "description_json", "description_binary")
        ):
            return Response(
                {"error": "Folders cannot contain document content"},
                status=status.HTTP_400_BAD_REQUEST,
            ), None

        access = data.get("access", page.access)
        parent_id = data.get("parent", page.parent_id)
        if "parent" in data or (page.parent_id and access != page.access):
            parent, parent_error = validate_page_parent(
                self.kwargs["slug"],
                project_id,
                parent_id,
                page_id=page.id,
                access=access,
                user=request.user,
            )
            if parent_error:
                return parent_error, None
            data["parent"] = parent.id if parent else None

        if page.node_type == Page.FOLDER_NODE and ("name" in data or "parent" in data):
            name_error = validate_sibling_folder_name(
                self.kwargs["slug"],
                project_id,
                data.get("name", page.name),
                data.get("parent", page.parent_id),
                access,
                exclude_page_id=page.id,
            )
            if name_error:
                return name_error, None

        if (
            access != page.access
            and page.node_type == Page.FOLDER_NODE
            and page.child_page.filter(deleted_at__isnull=True).exclude(access=access).exists()
        ):
            return Response(
                {"error": "Move or update folder children before changing access"},
                status=status.HTTP_400_BAD_REQUEST,
            ), None
        return None, data

    def update_page(self, request, page):
        if page.is_locked:
            return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)
        if "access" in request.data and request.data["access"] != page.access and page.owned_by_id != request.user.id:
            return Response(
                {"error": "Only the page owner can change access"},
                status=status.HTTP_403_FORBIDDEN,
            )

        hierarchy_error, data = self.validate_hierarchy_update(request, page)
        if hierarchy_error:
            return hierarchy_error

        old_description_html = page.description_html
        serializer = PublicPageSerializer(page, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        converted_document = None
        converted_binary = None
        if "description_html" in serializer.validated_data:
            replace_url = os.environ.get("PLANE_YJS_REPLACE_URL", "http://live:3001")
            live_secret = os.environ.get("LIVE_SERVER_SECRET_KEY")
            if not live_secret:
                return Response(
                    {"error": "Plane Live replacement authentication is not configured"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            try:
                conversion = requests.post(
                    f"{replace_url.rstrip('/')}/replace-document",
                    json={
                        "base_binary": base64.b64encode(page.description_binary or b"").decode("ascii"),
                        "description_html": serializer.validated_data["description_html"],
                    },
                    headers={"live-server-secret-key": live_secret},
                    timeout=30,
                )
                conversion.raise_for_status()
                converted_document = conversion.json()
                converted_binary = base64.b64decode(converted_document["description_binary"], validate=True)
                converted_document["description_json"]
            except (requests.RequestException, ValueError, KeyError, binascii.Error):
                return Response(
                    {"error": "Plane Live could not convert the page document"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        page = serializer.save(updated_by=request.user)
        if (
            "description_html" in serializer.validated_data
            and converted_document is not None
            and converted_binary is not None
        ):
            # Keep every Plane editor representation in sync. Updating HTML
            # alone leaves the collaborative editor on its previous Yjs state.
            # The replacement binary also carries Yjs deletion tombstones, so
            # an older connected client cannot merge removed pipe text back in.
            page.description_binary = converted_binary
            page.description_json = converted_document["description_json"]
            page.description_html = converted_document["description_html"]
            page.save(
                update_fields=[
                    "description_binary",
                    "description_json",
                    "description_html",
                    "updated_at",
                ]
            )
            page_transaction.delay(
                new_description_html=page.description_html,
                old_description_html=old_description_html,
                page_id=page.id,
            )
        return Response(PublicPageSerializer(page).data, status=status.HTTP_200_OK)


class WorkspacePageListCreateAPIEndpoint(PageQuerysetMixin, BaseAPIView):
    permission_classes = [WorkspaceEntityPermission]

    def get_queryset(self):
        return self.visible_pages().filter(
            Q(is_global=True)
            | Q(
                project_pages__project__project_projectmember__member=self.request.user,
                project_pages__project__project_projectmember__is_active=True,
                project_pages__project__archived_at__isnull=True,
                project_pages__deleted_at__isnull=True,
            )
        )

    def get(self, request, slug):
        return self.paginate(
            request=request,
            queryset=self.get_queryset().order_by("-created_at"),
            on_results=lambda pages: (
                PublicPageSerializer(pages, many=True, fields=self.fields, expand=self.expand).data
            ),
        )

    def post(self, request, slug):
        serializer = PublicPageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = Workspace.objects.get(
            slug=slug,
            workspace_member__member=request.user,
            workspace_member__is_active=True,
        )
        page = serializer.save(
            workspace=workspace,
            owned_by=request.user,
            is_global=True,
            created_by=request.user,
        )
        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=page.id,
        )
        return Response(PublicPageSerializer(page).data, status=status.HTTP_201_CREATED)


class WorkspacePageDetailAPIEndpoint(PageQuerysetMixin, BaseAPIView):
    permission_classes = [WorkspaceEntityPermission]

    def get_queryset(self):
        return self.visible_pages().filter(
            Q(is_global=True)
            | Q(
                project_pages__project__project_projectmember__member=self.request.user,
                project_pages__project__project_projectmember__is_active=True,
                project_pages__project__archived_at__isnull=True,
                project_pages__deleted_at__isnull=True,
            )
        )

    def get(self, request, slug, page_id):
        page = self.get_queryset().get(pk=page_id)
        return Response(
            PublicPageSerializer(page, fields=self.fields, expand=self.expand).data,
            status=status.HTTP_200_OK,
        )

    def patch(self, request, slug, page_id):
        page = self.get_queryset().get(pk=page_id)
        return self.update_page(request, page)


class ProjectPageListCreateAPIEndpoint(PageQuerysetMixin, BaseAPIView):
    permission_classes = [ProjectPagePermission]

    def get_queryset(self):
        project = Project.objects.get(
            pk=self.kwargs["project_id"],
            workspace__slug=self.kwargs["slug"],
            archived_at__isnull=True,
        )
        queryset = self.visible_pages().filter(
            project_pages__project_id=self.kwargs["project_id"],
            project_pages__deleted_at__isnull=True,
            project_pages__project__archived_at__isnull=True,
        )
        is_guest = ProjectMember.objects.filter(
            project=project,
            member=self.request.user,
            role=5,
            is_active=True,
        ).exists()
        if is_guest and not project.guest_view_all_features:
            queryset = queryset.filter(owned_by=self.request.user)
        return queryset

    def get(self, request, slug, project_id):
        queryset = self.filter_page_tree(request, self.get_queryset())
        if isinstance(queryset, Response):
            return queryset
        queryset = (
            self.hierarchy_order(queryset)
            if "parent" in request.query_params or request.query_params.get("folders_only")
            else queryset.order_by("-created_at")
        )
        return self.paginate(
            request=request,
            queryset=queryset,
            on_results=lambda pages: (
                PublicPageSerializer(pages, many=True, fields=self.fields, expand=self.expand).data
            ),
        )

    def post(self, request, slug, project_id):
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        node_type = data.get("node_type", Page.PAGE_NODE)
        if node_type not in (Page.PAGE_NODE, Page.FOLDER_NODE):
            return Response({"error": "Invalid node_type"}, status=status.HTTP_400_BAD_REQUEST)
        access = data.get("access", Page.PUBLIC_ACCESS)
        parent, parent_error = validate_page_parent(
            slug,
            project_id,
            data.get("parent"),
            access=access,
            user=request.user,
        )
        if parent_error:
            return parent_error
        data["parent"] = parent.id if parent else None
        if node_type == Page.FOLDER_NODE:
            name_error = validate_sibling_folder_name(
                slug, project_id, data.get("name"), parent.id if parent else None, access
            )
            if name_error:
                return name_error
            data["description_html"] = "<p></p>"

        serializer = PublicPageSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        project = Project.objects.get(pk=project_id, workspace__slug=slug, archived_at__isnull=True)

        external_id = serializer.validated_data.get("external_id")
        external_source = serializer.validated_data.get("external_source")
        existing = None
        if external_id:
            existing = Page.objects.filter(
                workspace=project.workspace,
                project_pages__project=project,
                project_pages__deleted_at__isnull=True,
                external_id=external_id,
                external_source=external_source,
            ).first()
        if existing:
            return Response(
                {"error": "Page with the same external id and source already exists", "id": str(existing.id)},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            page = serializer.save(
                workspace=project.workspace,
                owned_by=request.user,
                created_by=request.user,
            )
            ProjectPage.objects.create(
                workspace=project.workspace,
                project=project,
                page=page,
                created_by=request.user,
            )

        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=page.id,
        )
        return Response(PublicPageSerializer(page).data, status=status.HTTP_201_CREATED)


class ProjectPageDetailAPIEndpoint(PageQuerysetMixin, BaseAPIView):
    permission_classes = [ProjectPagePermission]

    def get_queryset(self):
        return self.visible_pages().filter(
            project_pages__project_id=self.kwargs["project_id"],
            project_pages__deleted_at__isnull=True,
            project_pages__project__archived_at__isnull=True,
        )

    def get(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)
        return Response(
            PublicPageSerializer(page, fields=self.fields, expand=self.expand).data,
            status=status.HTTP_200_OK,
        )

    def patch(self, request, slug, project_id, page_id):
        page = self.get_queryset().get(pk=page_id)
        return self.update_page(request, page)


class ProjectPageArchiveAPIEndpoint(PageQuerysetMixin, BaseAPIView):
    permission_classes = [ProjectPagePermission]

    def get_page(self, request, slug, project_id, page_id):
        page = self.visible_pages().get(
            pk=page_id,
            project_pages__project_id=project_id,
            project_pages__deleted_at__isnull=True,
        )
        is_admin = ProjectMember.objects.filter(
            project_id=project_id, member=request.user, role=20, is_active=True
        ).exists()
        if page.owned_by_id != request.user.id and not is_admin:
            return None, Response(
                {"error": "Only the owner or admin can archive the page"},
                status=status.HTTP_403_FORBIDDEN,
            )
        return page, None

    def post(self, request, slug, project_id, page_id):
        page, error = self.get_page(request, slug, project_id, page_id)
        if error:
            return error
        archived_at = timezone.now()
        unarchive_archive_page_and_descendants(page.id, archived_at)
        return Response({"archived_at": archived_at}, status=status.HTTP_200_OK)

    def delete(self, request, slug, project_id, page_id):
        page, error = self.get_page(request, slug, project_id, page_id)
        if error:
            return error
        if page.parent_id and page.parent.archived_at:
            page.parent = None
            page.save(update_fields=["parent"])
        unarchive_archive_page_and_descendants(page.id, None)
        return Response(status=status.HTTP_204_NO_CONTENT)
