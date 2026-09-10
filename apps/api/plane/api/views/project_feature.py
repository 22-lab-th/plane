# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only

from rest_framework import status
from rest_framework.response import Response

from plane.api.serializers.project_feature import ProjectFeatureSerializer
from plane.api.views.base import BaseAPIView
from plane.app.permissions import ProjectBasePermission
from plane.db.models import Intake, Project


FEATURE_FIELDS = {
    "modules": "module_view",
    "cycles": "cycle_view",
    "views": "issue_views_view",
    "pages": "page_view",
    "intakes": "intake_view",
    "work_item_types": "is_issue_type_enabled",
}


def serialize_features(project):
    return {public: getattr(project, model) for public, model in FEATURE_FIELDS.items()}


class ProjectFeatureAPIEndpoint(BaseAPIView):
    permission_classes = [ProjectBasePermission]
    use_read_replica = True

    def get_project(self, slug, project_id):
        return Project.objects.get(pk=project_id, workspace__slug=slug, archived_at__isnull=True)

    def get(self, request, slug, project_id):
        return Response(serialize_features(self.get_project(slug, project_id)), status=status.HTTP_200_OK)

    def patch(self, request, slug, project_id):
        unsupported = sorted(set(request.data) - set(FEATURE_FIELDS))
        if unsupported:
            return Response(
                {"error": f"Unsupported feature fields: {', '.join(unsupported)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = ProjectFeatureSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(
                {"error": "At least one feature field is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project = self.get_project(slug, project_id)
        changed = []
        for public, value in serializer.validated_data.items():
            model_field = FEATURE_FIELDS[public]
            setattr(project, model_field, value)
            changed.append(model_field)
        project.save(update_fields=[*changed, "updated_at"])

        if serializer.validated_data.get("intakes"):
            Intake.objects.get_or_create(
                project=project,
                is_default=True,
                defaults={"name": f"{project.name} Intake", "workspace": project.workspace},
            )

        return Response(serialize_features(project), status=status.HTTP_200_OK)
