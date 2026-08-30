# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction
from django.db.models import Q
import requests
from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import ROLE, allow_permission
from plane.app.serializers import WorkspaceBookmarkGroupSerializer, WorkspaceBookmarkSerializer
from plane.db.models import Workspace, WorkspaceBookmark, WorkspaceBookmarkGroup
from plane.utils.url_metadata import URLMetadataError, fetch_url_metadata

from ..base import BaseViewSet


class WorkspaceBookmarkGroupViewSet(BaseViewSet):
    model = WorkspaceBookmarkGroup
    use_read_replica = True

    def get_serializer_class(self):
        return WorkspaceBookmarkGroupSerializer

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def list(self, request, slug):
        groups = WorkspaceBookmarkGroup.objects.filter(workspace__slug=slug).order_by("sort_order", "name")
        return Response(WorkspaceBookmarkGroupSerializer(groups, many=True).data)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def retrieve(self, request, slug, pk):
        group = WorkspaceBookmarkGroup.objects.filter(workspace__slug=slug, pk=pk).first()
        if not group:
            return Response({"detail": "Bookmark group not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(WorkspaceBookmarkGroupSerializer(group).data)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def create(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        serializer = WorkspaceBookmarkGroupSerializer(
            data=request.data,
            context={"workspace": workspace},
        )
        if serializer.is_valid():
            serializer.save(workspace=workspace)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def partial_update(self, request, slug, pk):
        group = WorkspaceBookmarkGroup.objects.filter(workspace__slug=slug, pk=pk).first()
        if not group:
            return Response({"detail": "Bookmark group not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkspaceBookmarkGroupSerializer(
            group,
            data=request.data,
            partial=True,
            context={"workspace": group.workspace},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    @transaction.atomic
    def destroy(self, request, slug, pk):
        group = WorkspaceBookmarkGroup.objects.select_for_update().filter(workspace__slug=slug, pk=pk).first()
        if not group:
            return Response({"detail": "Bookmark group not found."}, status=status.HTTP_404_NOT_FOUND)
        WorkspaceBookmark.objects.filter(workspace=group.workspace, group=group).update(group=None)
        WorkspaceBookmarkGroup.objects.filter(pk=group.pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceBookmarkViewSet(BaseViewSet):
    model = WorkspaceBookmark
    use_read_replica = True

    def get_serializer_class(self):
        return WorkspaceBookmarkSerializer

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def metadata(self, request, slug):
        url = request.data.get("url", "").strip()
        if not url:
            return Response({"detail": "URL is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(fetch_url_metadata(url))
        except URLMetadataError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except ValueError:
            return Response(
                {"detail": "This URL cannot be accessed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except requests.RequestException:
            return Response(
                {"detail": "The page could not be reached."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def list(self, request, slug):
        bookmarks = WorkspaceBookmark.objects.filter(workspace__slug=slug).select_related("group")
        group_id = request.query_params.get("group")
        if group_id == "ungrouped":
            bookmarks = bookmarks.filter(group__isnull=True)
        elif group_id:
            bookmarks = bookmarks.filter(group_id=group_id)

        query = request.query_params.get("q", "").strip()
        if query:
            bookmarks = bookmarks.filter(
                Q(title__icontains=query) | Q(url__icontains=query) | Q(remark__icontains=query)
            )
        bookmarks = bookmarks.order_by("group__sort_order", "group__name", "sort_order", "title")
        return Response(WorkspaceBookmarkSerializer(bookmarks, many=True).data)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def retrieve(self, request, slug, pk):
        bookmark = WorkspaceBookmark.objects.filter(workspace__slug=slug, pk=pk).first()
        if not bookmark:
            return Response({"detail": "Bookmark not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(WorkspaceBookmarkSerializer(bookmark).data)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def create(self, request, slug):
        workspace = Workspace.objects.get(slug=slug)
        serializer = WorkspaceBookmarkSerializer(
            data=request.data,
            context={"workspace": workspace},
        )
        if serializer.is_valid():
            serializer.save(workspace=workspace)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def partial_update(self, request, slug, pk):
        bookmark = WorkspaceBookmark.objects.filter(workspace__slug=slug, pk=pk).first()
        if not bookmark:
            return Response({"detail": "Bookmark not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = WorkspaceBookmarkSerializer(
            bookmark,
            data=request.data,
            partial=True,
            context={"workspace": bookmark.workspace},
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def destroy(self, request, slug, pk):
        bookmark = WorkspaceBookmark.objects.filter(workspace__slug=slug, pk=pk).first()
        if not bookmark:
            return Response({"detail": "Bookmark not found."}, status=status.HTTP_404_NOT_FOUND)
        WorkspaceBookmark.objects.filter(pk=bookmark.pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
