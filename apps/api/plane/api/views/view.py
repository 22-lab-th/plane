# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from plane.api.serializers.view import PublicIssueViewSerializer
from plane.api.views.base import BaseAPIView
from plane.app.permissions import ProjectEntityPermission
from plane.db.models import IssueView, Project, ProjectMember


class ProjectViewQuerysetMixin:
    serializer_class = PublicIssueViewSerializer
    model = IssueView
    permission_classes = [ProjectEntityPermission]
    use_read_replica = True

    def get_queryset(self):
        return (
            IssueView.objects.filter(
                workspace__slug=self.kwargs["slug"],
                project_id=self.kwargs["project_id"],
                project__archived_at__isnull=True,
                archived_at__isnull=True,
            )
            .filter(Q(owned_by=self.request.user) | Q(access=1))
            .select_related("workspace", "project", "owned_by")
            .order_by("name")
            .distinct()
        )


class ProjectViewListCreateAPIEndpoint(ProjectViewQuerysetMixin, BaseAPIView):
    def get(self, request, slug, project_id):
        return self.paginate(
            request=request,
            queryset=self.get_queryset(),
            on_results=lambda views: (
                PublicIssueViewSerializer(views, many=True, fields=self.fields, expand=self.expand).data
            ),
        )

    def post(self, request, slug, project_id):
        serializer = PublicIssueViewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = Project.objects.get(pk=project_id, workspace__slug=slug, archived_at__isnull=True)
        issue_view = serializer.save(
            workspace=project.workspace,
            project=project,
            owned_by=request.user,
            created_by=request.user,
        )
        return Response(PublicIssueViewSerializer(issue_view).data, status=status.HTTP_201_CREATED)


class ProjectViewDetailAPIEndpoint(ProjectViewQuerysetMixin, BaseAPIView):
    def get(self, request, slug, project_id, view_id):
        issue_view = self.get_queryset().get(pk=view_id)
        return Response(
            PublicIssueViewSerializer(issue_view, fields=self.fields, expand=self.expand).data,
            status=status.HTTP_200_OK,
        )

    def patch(self, request, slug, project_id, view_id):
        issue_view = self.get_queryset().get(pk=view_id)
        if issue_view.is_locked:
            return Response({"error": "View is locked"}, status=status.HTTP_400_BAD_REQUEST)
        if issue_view.owned_by_id != request.user.id:
            return Response(
                {"error": "Only the view owner can update it"},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = PublicIssueViewSerializer(issue_view, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        issue_view = serializer.save(updated_by=request.user)
        return Response(PublicIssueViewSerializer(issue_view).data, status=status.HTTP_200_OK)

    def delete(self, request, slug, project_id, view_id):
        issue_view = IssueView.objects.get(pk=view_id, project_id=project_id, workspace__slug=slug)
        is_admin = ProjectMember.objects.filter(
            project_id=project_id,
            member=request.user,
            role=20,
            is_active=True,
        ).exists()
        if issue_view.owned_by_id != request.user.id and not is_admin:
            return Response(
                {"error": "Only a project admin or the view owner can delete it"},
                status=status.HTTP_403_FORBIDDEN,
            )
        issue_view.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
