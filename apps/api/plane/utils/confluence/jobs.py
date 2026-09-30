"""Durable job serialization, authorization and recovery shared by API and worker."""

from datetime import timedelta
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from plane.db.models import Page, Project, ProjectMember, User, WorkspaceMember
from plane.utils.confluence.client import ConfluenceError

ACTIVE = ("queued", "discovering", "running")


def require_active_run(run):
    if not type(run).objects.filter(pk=run.pk, status__in=ACTIVE).exists():
        raise ConfluenceError(
            "run_interrupted", "This run was interrupted. Its late results will not replace destination data."
        )
    require_import_permission(run.initiated_by, run.source.project, run.source)


def require_import_permission(user, project, source=None):
    if not User.objects.filter(pk=user.pk, is_active=True).exists():
        raise PermissionDenied("The importing account is inactive.")
    member = ProjectMember.objects.filter(project=project, member=user, is_active=True).first()
    if (
        not member
        or not WorkspaceMember.objects.filter(workspace=project.workspace, member=user, is_active=True).exists()
    ):
        raise PermissionDenied("Active workspace and project membership are required.")
    if (
        member.role < 15
        and not WorkspaceMember.objects.filter(
            workspace=project.workspace, member=user, role=20, is_active=True
        ).exists()
    ):
        raise PermissionDenied("Project member or administrator access is required.")
    if not Project.objects.filter(pk=project.pk, archived_at__isnull=True, workspace__deleted_at__isnull=True).exists():
        raise PermissionDenied("The project or workspace was archived or removed.")
    if source and source.access == Page.PRIVATE_ACCESS and source.owner_id != user.id:
        raise PermissionDenied("Only the owner may access this private import.")


def recover_stale_runs(queryset):
    for run in queryset.filter(status__in=ACTIVE, updated_at__lt=timezone.now() - timedelta(minutes=15)):
        changed = queryset.filter(pk=run.pk, status__in=ACTIVE, updated_at=run.updated_at).update(
            status="failed",
            phase="Worker interrupted",
            error_code="worker_interrupted",
            error_message="The worker stopped reporting progress. Retry the run; completed destinations are retained.",
            finished_at=timezone.now(),
            updated_at=timezone.now(),
        )
        if changed:
            run.results.filter(status__in=["pending", "running"]).update(
                status="failed",
                error_code="worker_interrupted",
                error_message="The worker was interrupted before this item completed.",
            )


def serialize_run(run):
    counters = {key: 0 for key in ("total", "completed", "failed", "skipped", "pending", "running")}
    types = {}
    for row in run.results.values("item__category", "status").annotate(count=Count("id")):
        category = row["item__category"]
        counts = types.setdefault(category, {key: 0 for key in counters})
        counts["total"] += row["count"]
        counts[row["status"]] += row["count"]
        counters["total"] += row["count"]
        counters[row["status"]] += row["count"]
    return {
        "id": str(run.id),
        "source_id": str(run.source_id),
        "space_id": run.source.space_id,
        "space_name": run.source.space_name,
        "mode": run.mode,
        "status": run.status,
        "phase": run.phase,
        "inventory_complete": run.inventory_complete,
        "counts": counters,
        "types": types,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def error_details(exc):
    if isinstance(exc, ConfluenceError):
        return exc.code, exc.message[:2000]
    if isinstance(exc, PermissionDenied):
        return "permission_denied", str(exc.detail)
    from plane.utils.file_storage.errors import ProjectFileError

    if isinstance(exc, ProjectFileError):
        data = exc.as_response()
        return data.get("code", "plane_upload_failed"), data.get("error", "Plane rejected the file upload.")
    import requests

    if isinstance(exc, requests.RequestException):
        return "network_error", "The connection was interrupted while reading data. Retry this item."
    # Never disclose credentials, signed URLs or raw network exception text.
    return "import_error", "Plane could not process this item. Check worker/storage logs using this run ID, then retry."
