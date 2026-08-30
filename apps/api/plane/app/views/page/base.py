# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import json
from datetime import datetime
from django.core.serializers.json import DjangoJSONEncoder

# Django imports
from django.db import connection
from django.db.models import (
    Exists,
    OuterRef,
    Q,
    Value,
    UUIDField,
    Count,
    Case,
    When,
    IntegerField,
)
from django.http import StreamingHttpResponse
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField
from django.db.models.functions import Coalesce

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import allow_permission, ROLE
from plane.app.serializers import (
    PageSerializer,
    PageDetailSerializer,
    PageBinaryUpdateSerializer,
)
from plane.db.models import (
    Page,
    PageLog,
    UserFavorite,
    ProjectMember,
    ProjectPage,
    Project,
    UserRecentVisit,
)
from plane.utils.error_codes import ERROR_CODES

# Local imports
from ..base import BaseAPIView, BaseViewSet
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.bgtasks.recent_visited_task import recent_visited_task
from plane.bgtasks.copy_s3_object import copy_s3_objects_of_description_and_assets
from plane.app.permissions import ProjectPagePermission


def unarchive_archive_page_and_descendants(page_id, archived_at):
    # Your SQL query
    sql = """
    WITH RECURSIVE descendants AS (
        SELECT id FROM pages WHERE id = %s
        UNION ALL
        SELECT pages.id FROM pages, descendants WHERE pages.parent_id = descendants.id
    )
    UPDATE pages SET archived_at = %s WHERE id IN (SELECT id FROM descendants);
    """

    # Execute the SQL query
    with connection.cursor() as cursor:
        cursor.execute(sql, [page_id, archived_at])


def page_would_create_cycle(page_id, parent_id):
    """Return True when assigning parent_id would create a parent cycle."""
    if parent_id is None:
        return False
    if str(page_id) == str(parent_id):
        return True

    sql = """
    WITH RECURSIVE ancestors AS (
        SELECT id, parent_id FROM pages WHERE id = %s AND deleted_at IS NULL
        UNION ALL
        SELECT pages.id, pages.parent_id
        FROM pages
        INNER JOIN ancestors ON pages.id = ancestors.parent_id
        WHERE pages.deleted_at IS NULL
    )
    SELECT 1 FROM ancestors WHERE id = %s LIMIT 1;
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [parent_id, page_id])
        return cursor.fetchone() is not None


def get_project_page(slug, project_id, page_id):
    return Page.objects.get(
        pk=page_id,
        workspace__slug=slug,
        projects__id=project_id,
        project_pages__deleted_at__isnull=True,
    )


def validate_page_parent(slug, project_id, parent_id, page_id=None, access=None, user=None):
    """Validate parent folder constraints for create/update. Returns (parent, error_response)."""
    if parent_id in (None, "", "null"):
        return None, None

    try:
        parent = get_project_page(slug, project_id, parent_id)
    except Page.DoesNotExist:
        return None, Response({"error": "Parent folder not found"}, status=status.HTTP_400_BAD_REQUEST)

    # Private pages are visible only to their owner in Plane CE. Apply the same
    # rule while resolving parents so a caller cannot infer or move content into
    # another user's private folder by guessing its UUID.
    if user is not None and parent.access == Page.PRIVATE_ACCESS and parent.owned_by_id != user.id:
        return None, Response({"error": "Parent folder not found"}, status=status.HTTP_400_BAD_REQUEST)

    if parent.node_type != Page.FOLDER_NODE:
        return None, Response({"error": "Parent must be a folder"}, status=status.HTTP_400_BAD_REQUEST)

    if parent.archived_at is not None:
        return None, Response({"error": "Cannot move into an archived folder"}, status=status.HTTP_400_BAD_REQUEST)

    if access is not None and parent.access != access:
        return None, Response(
            {"error": "Page access must match parent folder access"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if page_id and page_would_create_cycle(page_id, parent.id):
        return None, Response({"error": "Cannot create a folder cycle"}, status=status.HTTP_400_BAD_REQUEST)

    return parent, None


def validate_sibling_folder_name(slug, project_id, name, parent_id, access, exclude_page_id=None):
    """Reject duplicate folder names among siblings in the same access bucket."""
    trimmed = (name or "").strip()
    if not trimmed:
        return Response({"error": "Folder name cannot be empty"}, status=status.HTTP_400_BAD_REQUEST)

    siblings = Page.objects.filter(
        workspace__slug=slug,
        projects__id=project_id,
        project_pages__deleted_at__isnull=True,
        node_type=Page.FOLDER_NODE,
        access=access,
        name__iexact=trimmed,
        archived_at__isnull=True,
    )
    if parent_id:
        siblings = siblings.filter(parent_id=parent_id)
    else:
        siblings = siblings.filter(parent__isnull=True)
    if exclude_page_id:
        siblings = siblings.exclude(pk=exclude_page_id)
    if siblings.exists():
        return Response({"error": "A folder with this name already exists"}, status=status.HTTP_400_BAD_REQUEST)
    return None


class PageViewSet(BaseViewSet):
    serializer_class = PageSerializer
    model = Page
    permission_classes = [ProjectPagePermission]
    search_fields = ["name"]

    def get_base_queryset(self, roots_only=False):
        subquery = UserFavorite.objects.filter(
            user=self.request.user,
            entity_type="page",
            entity_identifier=OuterRef("pk"),
            workspace__slug=self.kwargs.get("slug"),
        )
        queryset = (
            super()
            .get_queryset()
            .filter(workspace__slug=self.kwargs.get("slug"))
            .filter(
                projects__project_projectmember__member=self.request.user,
                projects__project_projectmember__is_active=True,
                projects__archived_at__isnull=True,
            )
            .filter(Q(owned_by=self.request.user) | Q(access=0))
            .prefetch_related("projects")
            .select_related("workspace")
            .select_related("owned_by")
            .select_related("parent")
            .annotate(is_favorite=Exists(subquery))
            .prefetch_related("labels")
            .annotate(
                project=Exists(
                    ProjectPage.objects.filter(page_id=OuterRef("id"), project_id=self.kwargs.get("project_id"))
                )
            )
            .annotate(
                label_ids=Coalesce(
                    ArrayAgg(
                        "page_labels__label_id",
                        distinct=True,
                        filter=~Q(page_labels__label_id__isnull=True),
                    ),
                    Value([], output_field=ArrayField(UUIDField())),
                ),
                project_ids=Coalesce(
                    ArrayAgg("projects__id", distinct=True, filter=~Q(projects__id=True)),
                    Value([], output_field=ArrayField(UUIDField())),
                ),
            )
            .filter(project=True)
            .distinct()
        )
        if roots_only:
            queryset = queryset.filter(parent__isnull=True)
        return self.filter_queryset(queryset)

    def get_queryset(self):
        # Default list remains root-only; retrieve/detail use get_base_queryset.
        return self.get_base_queryset(roots_only=True)

    def create(self, request, slug, project_id):
        node_type = request.data.get("node_type", Page.PAGE_NODE)
        if node_type not in (Page.PAGE_NODE, Page.FOLDER_NODE):
            return Response({"error": "Invalid node_type"}, status=status.HTTP_400_BAD_REQUEST)

        access = request.data.get("access", Page.PUBLIC_ACCESS)
        parent_id = request.data.get("parent", None)
        parent, parent_error = validate_page_parent(slug, project_id, parent_id, access=access, user=request.user)
        if parent_error:
            return parent_error

        if node_type == Page.FOLDER_NODE:
            name_error = validate_sibling_folder_name(
                slug, project_id, request.data.get("name"), parent.id if parent else None, access
            )
            if name_error:
                return name_error

        description_html = (
            "<p></p>" if node_type == Page.FOLDER_NODE else request.data.get("description_html", "<p></p>")
        )
        serializer = PageSerializer(
            data=request.data,
            context={
                "project_id": project_id,
                "owned_by_id": request.user.id,
                "description_json": {} if node_type == Page.FOLDER_NODE else request.data.get("description_json", {}),
                "description_binary": (
                    None if node_type == Page.FOLDER_NODE else request.data.get("description_binary", None)
                ),
                "description_html": description_html,
            },
        )

        if serializer.is_valid():
            serializer.save()
            # capture the page transaction
            if node_type == Page.PAGE_NODE:
                page_transaction.delay(
                    new_description_html=description_html,
                    old_description_html=None,
                    page_id=serializer.data["id"],
                )
            page = self.get_base_queryset(roots_only=False).get(pk=serializer.data["id"])
            serializer = PageDetailSerializer(page)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def partial_update(self, request, slug, project_id, page_id):
        try:
            page = get_project_page(slug, project_id, page_id)

            if page.is_locked:
                return Response({"error": "Page is locked"}, status=status.HTTP_400_BAD_REQUEST)

            data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)

            if page.node_type == Page.FOLDER_NODE and any(
                field in data for field in ("description_html", "description_json", "description_binary")
            ):
                return Response(
                    {"error": "Folders cannot contain document content"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Folders cannot become pages (or vice versa) once created.
            if "node_type" in data and data.get("node_type") != page.node_type:
                return Response({"error": "node_type cannot be changed"}, status=status.HTTP_400_BAD_REQUEST)

            access = data.get("access", page.access)
            parent_provided = "parent" in data
            parent_id = data.get("parent") if parent_provided else page.parent_id
            if parent_provided:
                parent, parent_error = validate_page_parent(
                    slug,
                    project_id,
                    parent_id,
                    page_id=page_id,
                    access=access,
                    user=request.user,
                )
                if parent_error:
                    return parent_error
                data["parent"] = parent.id if parent else None
            elif page.parent_id and access != page.access:
                # Access change while nested must stay aligned with parent folder.
                parent, parent_error = validate_page_parent(
                    slug,
                    project_id,
                    page.parent_id,
                    page_id=page_id,
                    access=access,
                    user=request.user,
                )
                if parent_error:
                    return parent_error

            if page.node_type == Page.FOLDER_NODE and "name" in data:
                name_error = validate_sibling_folder_name(
                    slug,
                    project_id,
                    data.get("name"),
                    data.get("parent") if parent_provided else page.parent_id,
                    access,
                    exclude_page_id=page_id,
                )
                if name_error:
                    return name_error

            # Only update access if the page owner is the requesting  user
            if page.access != data.get("access", page.access) and page.owned_by_id != request.user.id:
                return Response(
                    {"error": "Access cannot be updated since this page is owned by someone else"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serializer = PageDetailSerializer(page, data=data, partial=True)
            page_description = page.description_html
            if serializer.is_valid():
                serializer.save()
                # capture the page transaction
                if data.get("description_html") and page.node_type == Page.PAGE_NODE:
                    page_transaction.delay(
                        new_description_html=data.get("description_html", "<p></p>"),
                        old_description_html=page_description,
                        page_id=page_id,
                    )

                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Page.DoesNotExist:
            return Response(
                {"error": "Page not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

    def retrieve(self, request, slug, project_id, page_id=None):
        page = self.get_base_queryset(roots_only=False).filter(pk=page_id).first()
        project = Project.objects.get(pk=project_id)
        track_visit = request.query_params.get("track_visit", "true").lower() == "true"

        if page is None:
            return Response({"error": "Page not found"}, status=status.HTTP_404_NOT_FOUND)

        """
        if the role is guest and guest_view_all_features is false and owned by is not
        the requesting user then dont show the page
        """

        if (
            ProjectMember.objects.filter(
                workspace__slug=slug,
                project_id=project_id,
                member=request.user,
                role=5,
                is_active=True,
            ).exists()
            and not project.guest_view_all_features
            and not page.owned_by == request.user
        ):
            return Response(
                {"error": "You are not allowed to view this page"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        issue_ids = PageLog.objects.filter(page_id=page_id, entity_name="issue").values_list(
            "entity_identifier", flat=True
        )
        data = PageDetailSerializer(page).data
        data["issue_ids"] = issue_ids
        if track_visit:
            recent_visited_task.delay(
                slug=slug,
                entity_name="page",
                entity_identifier=page_id,
                user_id=request.user.id,
                project_id=project_id,
            )
        return Response(data, status=status.HTTP_200_OK)

    def lock(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        page.is_locked = True
        page.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def unlock(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        page.is_locked = False
        page.save()

        return Response(status=status.HTTP_204_NO_CONTENT)

    def access(self, request, slug, project_id, page_id):
        access = request.data.get("access", 0)
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        # Only update access if the page owner is the requesting user
        if page.access != request.data.get("access", page.access) and page.owned_by_id != request.user.id:
            return Response(
                {"error": "Access cannot be updated since this page is owned by someone else"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if access != page.access and page.parent_id and page.parent.access != access:
            return Response(
                {"error": "Page access must match parent folder access"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            access != page.access
            and page.node_type == Page.FOLDER_NODE
            and page.child_page.filter(deleted_at__isnull=True).exclude(access=access).exists()
        ):
            return Response(
                {"error": "Move or update folder children before changing access"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        page.access = access
        page.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, slug, project_id):
        parent_param = request.query_params.get("parent", None)
        folders_only = request.query_params.get("folders_only", "false").lower() == "true"
        queryset = self.get_base_queryset(roots_only=False)

        if folders_only:
            queryset = queryset.filter(node_type=Page.FOLDER_NODE)
        elif parent_param in (None, "", "null", "root"):
            queryset = queryset.filter(parent__isnull=True)
        else:
            parent, parent_error = validate_page_parent(slug, project_id, parent_param, user=request.user)
            if parent_error:
                return parent_error
            queryset = queryset.filter(parent_id=parent.id)

        project = Project.objects.get(pk=project_id)
        if (
            ProjectMember.objects.filter(
                workspace__slug=slug,
                project_id=project_id,
                member=request.user,
                role=5,
                is_active=True,
            ).exists()
            and not project.guest_view_all_features
        ):
            queryset = queryset.filter(owned_by=request.user)

        queryset = queryset.order_by(
            Case(
                When(node_type=Page.FOLDER_NODE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            "sort_order",
            "name",
            "-is_favorite",
            "-created_at",
            "id",
        )
        pages = PageSerializer(queryset, many=True).data
        return Response(pages, status=status.HTTP_200_OK)

    def archive(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        # only the owner or admin can archive the page
        if (
            ProjectMember.objects.filter(
                project_id=project_id, member=request.user, is_active=True, role__lte=15
            ).exists()
            and request.user.id != page.owned_by_id
        ):
            return Response(
                {"error": "Only the owner or admin can archive the page"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        UserFavorite.objects.filter(
            entity_type="page",
            entity_identifier=page_id,
            project_id=project_id,
            workspace__slug=slug,
        ).delete()

        unarchive_archive_page_and_descendants(page_id, datetime.now())

        return Response({"archived_at": str(datetime.now())}, status=status.HTTP_200_OK)

    def unarchive(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        # only the owner or admin can un archive the page
        if (
            ProjectMember.objects.filter(
                project_id=project_id, member=request.user, is_active=True, role__lte=15
            ).exists()
            and request.user.id != page.owned_by_id
        ):
            return Response(
                {"error": "Only the owner or admin can un archive the page"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # if parent archived then page will be un archived breaking hierarchy
        if page.parent_id and page.parent.archived_at:
            page.parent = None
            page.save(update_fields=["parent"])

        unarchive_archive_page_and_descendants(page_id, None)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def destroy(self, request, slug, project_id, page_id):
        page = get_project_page(slug, project_id, page_id)

        if page.archived_at is None:
            return Response(
                {"error": "The page should be archived before deleting"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if page.owned_by_id != request.user.id and (
            not ProjectMember.objects.filter(
                workspace__slug=slug,
                member=request.user,
                role=20,
                project_id=project_id,
                is_active=True,
            ).exists()
        ):
            return Response(
                {"error": "Only admin or owner can delete the page"},
                status=status.HTTP_403_FORBIDDEN,
            )

        children = Page.objects.filter(
            parent_id=page_id,
            projects__id=project_id,
            workspace__slug=slug,
            project_pages__deleted_at__isnull=True,
        )
        if page.node_type == Page.FOLDER_NODE and children.exists():
            return Response(
                {"error": "Folder must be empty before deleting"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # remove parent from all the children
        children.update(parent=None)

        page.delete()
        # Delete the user favorite page
        UserFavorite.objects.filter(
            project=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_type="page",
        ).delete()
        # Delete the page from recent visit
        UserRecentVisit.objects.filter(
            project_id=project_id,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_name="page",
        ).delete(soft=False)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def summary(self, request, slug, project_id):
        queryset = self.get_base_queryset(roots_only=False)

        project = Project.objects.get(pk=project_id)
        if (
            ProjectMember.objects.filter(
                workspace__slug=slug,
                project_id=project_id,
                member=request.user,
                role=ROLE.GUEST.value,
                is_active=True,
            ).exists()
            and not project.guest_view_all_features
        ):
            queryset = queryset.filter(owned_by=request.user)

        stats = queryset.aggregate(
            public_pages=Count(
                Case(
                    When(access=Page.PUBLIC_ACCESS, archived_at__isnull=True, node_type=Page.PAGE_NODE, then=1),
                    output_field=IntegerField(),
                )
            ),
            private_pages=Count(
                Case(
                    When(access=Page.PRIVATE_ACCESS, archived_at__isnull=True, node_type=Page.PAGE_NODE, then=1),
                    output_field=IntegerField(),
                )
            ),
            archived_pages=Count(
                Case(
                    When(archived_at__isnull=False, node_type=Page.PAGE_NODE, then=1),
                    output_field=IntegerField(),
                )
            ),
        )

        return Response(stats, status=status.HTTP_200_OK)


class PageFavoriteViewSet(BaseViewSet):
    model = UserFavorite

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def create(self, request, slug, project_id, page_id):
        _ = UserFavorite.objects.create(
            project_id=project_id,
            entity_identifier=page_id,
            entity_type="page",
            user=request.user,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER])
    def destroy(self, request, slug, project_id, page_id):
        page_favorite = UserFavorite.objects.get(
            project=project_id,
            user=request.user,
            workspace__slug=slug,
            entity_identifier=page_id,
            entity_type="page",
        )
        page_favorite.delete(soft=False)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PagesDescriptionViewSet(BaseViewSet):
    permission_classes = [ProjectPagePermission]

    def retrieve(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            Q(owned_by=self.request.user) | Q(access=0),
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )
        binary_data = page.description_binary

        def stream_data():
            if binary_data:
                yield binary_data
            else:
                yield b""

        response = StreamingHttpResponse(stream_data(), content_type="application/octet-stream")
        response["Content-Disposition"] = 'attachment; filename="page_description.bin"'
        return response

    def partial_update(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            Q(owned_by=self.request.user) | Q(access=0),
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        if page.is_locked:
            return Response(
                {
                    "error_code": ERROR_CODES["PAGE_LOCKED"],
                    "error_message": "PAGE_LOCKED",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if page.archived_at:
            return Response(
                {
                    "error_code": ERROR_CODES["PAGE_ARCHIVED"],
                    "error_message": "PAGE_ARCHIVED",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if page.node_type == Page.FOLDER_NODE:
            return Response(
                {"error": "Folders cannot contain document content"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Store the old description_html before saving (needed for both tasks)
        old_description_html = page.description_html

        # Serialize the existing instance
        existing_instance = json.dumps({"description_html": old_description_html}, cls=DjangoJSONEncoder)

        # Use serializer for validation and update
        serializer = PageBinaryUpdateSerializer(page, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()

            # Capture the page transaction
            if request.data.get("description_html"):
                page_transaction.delay(
                    new_description_html=request.data.get("description_html", "<p></p>"),
                    old_description_html=old_description_html,
                    page_id=page_id,
                )

            # Run background tasks
            track_page_version.delay(
                page_id=page_id,
                existing_instance=existing_instance,
                user_id=request.user.id,
            )
            return Response({"message": "Updated successfully"})
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PageDuplicateEndpoint(BaseAPIView):
    permission_classes = [ProjectPagePermission]

    def post(self, request, slug, project_id, page_id):
        page = Page.objects.get(
            pk=page_id,
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        )

        # check for permission
        if page.access == Page.PRIVATE_ACCESS and page.owned_by_id != request.user.id:
            return Response({"error": "Permission denied"}, status=status.HTTP_403_FORBIDDEN)

        # get all the project ids where page is present
        project_ids = ProjectPage.objects.filter(page_id=page_id).values_list("project_id", flat=True)

        original_parent_id = page.parent_id
        page.pk = None
        page.name = f"{page.name} (Copy)"
        page.description_binary = None
        page.owned_by = request.user
        page.created_by = request.user
        page.updated_by = request.user
        # Keep same-project folder placement; folders themselves are not duplicated into nested trees.
        page.parent_id = original_parent_id if page.node_type == Page.PAGE_NODE else None
        page.save()

        for project_id in project_ids:
            ProjectPage.objects.create(
                workspace_id=page.workspace_id,
                project_id=project_id,
                page_id=page.id,
                created_by_id=page.created_by_id,
                updated_by_id=page.updated_by_id,
            )

        page_transaction.delay(
            new_description_html=page.description_html,
            old_description_html=None,
            page_id=page.id,
        )

        # Copy the s3 objects uploaded in the page
        copy_s3_objects_of_description_and_assets.delay(
            entity_name="PAGE",
            entity_identifier=page.id,
            project_id=project_id,
            slug=slug,
            user_id=request.user.id,
        )

        page = (
            Page.objects.filter(pk=page.id)
            .annotate(
                project_ids=Coalesce(
                    ArrayAgg("projects__id", distinct=True, filter=~Q(projects__id=True)),
                    Value([], output_field=ArrayField(UUIDField())),
                )
            )
            .first()
        )
        serializer = PageDetailSerializer(page)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
