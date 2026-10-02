import re
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from plane.app.views.base import BaseAPIView
from plane.bgtasks.jira_import_task import jira_import_task
from plane.db.models import JiraRun, JiraSource, Project, ProjectMember, WorkspaceMember
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.jobs import ACTIVE, recover_stale_runs, require_import_permission
from plane.utils.jira.client import JiraClient
from plane.utils.jira.config import get_jira_config
from plane.utils.jira.jobs import serialize_run


class StartJiraImportSerializer(serializers.Serializer):
    remote_project_id = serializers.RegexField(r"^[0-9]+$", max_length=100, required=False)
    source_id = serializers.UUIDField(required=False)
    mode = serializers.ChoiceField(choices=["all", "changed", "failed", "selected"], default="changed")
    item_ids = serializers.ListField(child=serializers.UUIDField(), max_length=500, default=list)
    user_mapping = serializers.DictField(child=serializers.UUIDField(), required=False)

    def validate(self, data):
        if not data.get("source_id") and not data.get("remote_project_id"):
            raise serializers.ValidationError("Choose a Jira project.")
        if data["mode"] == "selected" and not data["item_ids"]:
            raise serializers.ValidationError("Select at least one item to retry.")
        mapping = data.get("user_mapping", {})
        if len(mapping) > 500 or any(not key or len(key) > 128 for key in mapping):
            raise serializers.ValidationError("At most 500 valid Jira account IDs may be mapped.")
        return data


def project_for(request, slug, project_id):
    project = get_object_or_404(Project.objects.select_related("workspace"), pk=project_id, workspace__slug=slug)
    require_import_permission(request.user, project)
    return project


class JiraProjectsEndpoint(BaseAPIView):
    def get(self, request, slug, project_id):
        project_for(request, slug, project_id)
        offset = request.query_params.get("offset", "0")
        if not re.fullmatch(r"[0-9]{1,9}", offset):
            return Response({"error": "Invalid offset."}, status=400)
        try:
            config = get_jira_config(require_enabled=True)
            data = JiraClient(config).json("/rest/api/3/project/search", {"maxResults": 100, "startAt": int(offset)})
            rows = data.get("values", [])
            next_offset = int(data.get("startAt", offset)) + len(rows)
            return Response(
                {
                    "site_url": config["site_url"],
                    "results": [{"id": str(row["id"]), "key": row["key"], "name": row["name"]} for row in rows],
                    "next_offset": next_offset
                    if rows and not data.get("isLast", next_offset >= data.get("total", next_offset))
                    else None,
                }
            )
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)


class JiraMembersEndpoint(BaseAPIView):
    def get(self, request, slug, project_id):
        project = project_for(request, slug, project_id)
        ids = WorkspaceMember.objects.filter(workspace=project.workspace, is_active=True).values("member_id")
        members = ProjectMember.objects.filter(
            project=project, is_active=True, member_id__in=ids, member__is_active=True
        ).select_related("member")
        return Response([{"id": str(row.member_id), "name": row.member.display_name} for row in members])


class JiraRunsEndpoint(BaseAPIView):
    def get(self, request, slug, project_id):
        project = project_for(request, slug, project_id)
        runs = JiraRun.objects.filter(source__project=project).select_related("source")
        recover_stale_runs(runs)
        return Response([serialize_run(run) for run in runs[:20]])

    def post(self, request, slug, project_id):
        project = project_for(request, slug, project_id)
        serializer = StartJiraImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        mapping = {key: str(value) for key, value in data.get("user_mapping", {}).items()}
        if mapping:
            workspace_ids = WorkspaceMember.objects.filter(workspace=project.workspace, is_active=True).values(
                "member_id"
            )
            valid_ids = set(
                str(value)
                for value in ProjectMember.objects.filter(
                    project=project,
                    member_id__in=workspace_ids,
                    is_active=True,
                    member__is_active=True,
                )
                .filter(member_id__in=mapping.values())
                .values_list("member_id", flat=True)
            )
            if set(mapping.values()) - valid_ids:
                return Response({"error": "User mappings must reference active members of this project."}, status=400)
        try:
            config = get_jira_config(require_enabled=True)
            if data.get("source_id"):
                source = get_object_or_404(JiraSource, pk=data["source_id"], project=project)
            else:
                remote = JiraClient(config).json(f"/rest/api/3/project/{data['remote_project_id']}")
                source, _ = JiraSource.objects.get_or_create(
                    project=project,
                    site_url=config["site_url"],
                    remote_project_id=data["remote_project_id"],
                    defaults={"project_name": remote["name"], "project_key": remote["key"], "owner": request.user},
                )
            if source.site_url != config["site_url"]:
                raise ConfluenceError("site_changed", "Restore this source's Jira site in God Mode before syncing.")
            selection = [str(value) for value in data["item_ids"]]
            if selection and source.items.filter(pk__in=selection).count() != len(set(selection)):
                return Response({"error": "Selected items must belong to this Jira import source."}, status=400)
            with transaction.atomic():
                source = JiraSource.objects.select_for_update().get(pk=source.pk)
                recover_stale_runs(source.runs.all())
                active = source.runs.filter(status__in=ACTIVE).first()
                if active:
                    return Response(
                        {"error": "An import is already running for this Jira project.", "run": serialize_run(active)},
                        status=409,
                    )
                if "user_mapping" in data:
                    source.user_mapping = mapping
                    source.save(update_fields=["user_mapping"])
                run = JiraRun.objects.create(
                    source=source, initiated_by=request.user, mode=data["mode"], selection=selection
                )
            try:
                jira_import_task.delay(str(run.pk))
            except Exception:
                run.status = "failed"
                run.error_code = "queue_unavailable"
                run.error_message = "The background queue is unavailable. Restore the worker/broker and retry."
                run.phase = "Could not enqueue import"
                run.finished_at = timezone.now()
                run.save()
                return Response({"error": run.error_message, "run": serialize_run(run)}, status=503)
            return Response(serialize_run(run), status=202)
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)


class JiraRunDetailEndpoint(BaseAPIView):
    def get(self, request, slug, project_id, run_id):
        project = project_for(request, slug, project_id)
        runs = JiraRun.objects.filter(source__project=project).select_related("source")
        recover_stale_runs(runs.filter(pk=run_id))
        run = get_object_or_404(runs, pk=run_id)
        offset = request.query_params.get("offset", "0")
        if not re.fullmatch(r"[0-9]{1,9}", offset):
            return Response({"error": "Invalid offset."}, status=400)
        offset = int(offset)
        items = run.results.select_related("item").order_by("item__kind", "item__title", "id")
        if request.query_params.get("status"):
            items = items.filter(status=request.query_params["status"])
        count = items.count()
        return Response(
            {
                "run": serialize_run(run),
                "count": count,
                "next_offset": offset + 100 if offset + 100 < count else None,
                "user_mapping": run.source.user_mapping,
                "results": [
                    {
                        "id": str(row.item_id),
                        "remote_id": row.item.remote_id,
                        "kind": row.item.kind,
                        "category": row.item.category,
                        "title": row.item.title,
                        "revision": row.revision,
                        "status": row.status,
                        "error_code": row.error_code,
                        "error_message": row.error_message,
                        "warning": row.warning,
                        "issue_id": str(row.item.issue_id) if row.item.issue_id else None,
                        "file_id": str(row.item.file_id) if row.item.file_id else None,
                        "cycle_id": str(row.item.cycle_id) if row.item.cycle_id else None,
                    }
                    for row in items[offset : offset + 100]
                ],
            }
        )
