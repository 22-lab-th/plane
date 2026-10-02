import re
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from plane.app.views.base import BaseAPIView
from plane.bgtasks.confluence_import_task import confluence_import_task
from plane.db.models import ConfluenceRun, ConfluenceSource, Page, Project
from plane.utils.confluence.client import ConfluenceClient, ConfluenceError
from plane.utils.confluence.config import get_confluence_config
from plane.utils.confluence.jobs import ACTIVE, recover_stale_runs, require_import_permission, serialize_run
from plane.utils.confluence.spaces import search_spaces


class StartImportSerializer(serializers.Serializer):
    space_id = serializers.RegexField(r"^[0-9]+$", max_length=100, required=False)
    source_id = serializers.UUIDField(required=False)
    mode = serializers.ChoiceField(choices=["all", "changed", "failed", "selected"], default="changed")
    item_ids = serializers.ListField(child=serializers.UUIDField(), max_length=500, default=list)
    parent_id = serializers.UUIDField(required=False, allow_null=True)
    access = serializers.ChoiceField(choices=[0, 1], default=0)

    def validate(self, data):
        if not data.get("source_id") and not data.get("space_id"):
            raise serializers.ValidationError("Choose a Confluence space.")
        if data["mode"] == "selected" and not data["item_ids"]:
            raise serializers.ValidationError("Select at least one item to retry.")
        return data


def project_for(request, slug, project_id):
    project = get_object_or_404(Project.objects.select_related("workspace"), pk=project_id, workspace__slug=slug)
    require_import_permission(request.user, project)
    return project


def visible_runs(request, project):
    return (
        ConfluenceRun.objects.filter(source__project=project)
        .filter(Q(source__access=0) | Q(source__owner=request.user))
        .select_related("source")
    )


class ConfluenceSpacesEndpoint(BaseAPIView):
    def get(self, request, slug, project_id):
        project_for(request, slug, project_id)
        try:
            config = get_confluence_config(require_enabled=True)
            cursor = request.query_params.get("cursor", "")
            if len(cursor) > 2048:
                return Response({"error": "Invalid cursor."}, status=400)
            query = request.query_params.get("search", "").strip()
            if len(query) > 200:
                return Response({"error": "Search must be 200 characters or fewer."}, status=400)
            client = ConfluenceClient(config)
            if query:
                return Response({"site_url": config["site_url"], **search_spaces(client, query, cursor)})
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = client.json("/wiki/api/v2/spaces", params)
            from urllib.parse import parse_qs, urlsplit

            next_cursor = (parse_qs(urlsplit(data.get("_links", {}).get("next", "")).query).get("cursor") or [None])[0]
            return Response(
                {
                    "site_url": config["site_url"],
                    "results": [
                        {"id": str(row["id"]), "name": row["name"], "key": row["key"]}
                        for row in data.get("results", [])
                    ],
                    "next_cursor": next_cursor,
                }
            )
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)


class ConfluenceRunsEndpoint(BaseAPIView):
    def get(self, request, slug, project_id):
        project = project_for(request, slug, project_id)
        runs = visible_runs(request, project)
        recover_stale_runs(runs)
        return Response([serialize_run(run) for run in runs[:20]])

    def post(self, request, slug, project_id):
        project = project_for(request, slug, project_id)
        serializer = StartImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            config = get_confluence_config(require_enabled=True)
            if data.get("source_id"):
                source = get_object_or_404(ConfluenceSource, pk=data["source_id"], project=project)
                require_import_permission(request.user, project, source)
                if source.site_url != config["site_url"]:
                    raise ConfluenceError(
                        "site_changed", "Restore the source's Atlassian site in God Mode before syncing."
                    )
            else:
                space = ConfluenceClient(config).json(f"/wiki/api/v2/spaces/{data['space_id']}")
                parent = None
                if data.get("parent_id"):
                    parent = get_object_or_404(
                        Page,
                        pk=data["parent_id"],
                        projects=project,
                        node_type=Page.FOLDER_NODE,
                        access=data["access"],
                        archived_at__isnull=True,
                        is_locked=False,
                    )
                    if parent.access == Page.PRIVATE_ACCESS and parent.owned_by_id != request.user.id:
                        return Response({"error": "Only the owner may import into this private folder."}, status=403)
                source, _ = ConfluenceSource.objects.get_or_create(
                    project=project,
                    site_url=config["site_url"],
                    space_id=data["space_id"],
                    defaults={
                        "space_name": space["name"],
                        "owner": request.user,
                        "access": data["access"],
                        "parent": parent,
                    },
                )
                require_import_permission(request.user, project, source)
            selection = [str(value) for value in data["item_ids"]]
            if selection and source.items.filter(pk__in=selection).count() != len(set(selection)):
                return Response({"error": "Selected items must belong to this import source."}, status=400)
            # Lock the source to serialize enqueue requests against the active-run constraint.
            with transaction.atomic():
                ConfluenceSource.objects.select_for_update().get(pk=source.pk)
                recover_stale_runs(source.runs.all())
                active = source.runs.filter(status__in=ACTIVE).first()
                if active:
                    return Response(
                        {"error": "An import is already running for this space.", "run": serialize_run(active)},
                        status=409,
                    )
                run = ConfluenceRun.objects.create(
                    source=source, initiated_by=request.user, mode=data["mode"], selection=selection
                )
            try:
                confluence_import_task.delay(str(run.pk))
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


class ConfluenceRunDetailEndpoint(BaseAPIView):
    def get(self, request, slug, project_id, run_id):
        project = project_for(request, slug, project_id)
        runs = visible_runs(request, project)
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
                "results": [
                    {
                        "id": str(row.item_id),
                        "remote_id": row.item.remote_id,
                        "kind": row.item.kind,
                        "category": row.item.category,
                        "title": row.item.title,
                        "version": row.version,
                        "status": row.status,
                        "error_code": row.error_code,
                        "error_message": row.error_message,
                        "page_id": str(row.item.page_id) if row.item.page_id else None,
                        "file_id": str(row.item.file_id) if row.item.file_id else None,
                    }
                    for row in items[offset : offset + 100]
                ],
            }
        )
