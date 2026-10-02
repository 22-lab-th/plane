"""Jira imports persist inventory and every outcome outside the HTTP request."""

import logging
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from plane.db.models import JiraItem, JiraRun, JiraRunItem
from plane.utils.confluence.client import ConfluenceError, category_for
from plane.utils.confluence.jobs import ACTIVE, error_details, require_active_run, require_import_permission
from plane.utils.confluence.storage import store_attachment
from plane.utils.jira.client import JiraClient
from plane.utils.jira.config import get_jira_config
from plane.utils.jira.content import revision
from plane.utils.jira.destinations import (
    prepare_issue,
    save_comment,
    save_issue,
    save_relationships,
    save_sprint,
    writable,
)

logger = logging.getLogger(__name__)


def heartbeat(run, phase):
    if not JiraRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(phase=phase, updated_at=timezone.now()):
        raise ConfluenceError("run_interrupted", "This run was interrupted; retry to continue.")
    require_import_permission(run.initiated_by, run.source.project)


def record_remote(run, kind, remote):
    if kind == "issue":
        title = f"{remote['key']}: {remote['fields'].get('summary', '')}"
        category = "issue: " + (remote["fields"].get("issuetype") or {}).get("name", "Work item")
    else:
        title = remote.get("filename") or remote.get("name") or remote.get("title") or f"Comment {remote['id']}"
        category = category_for("attachment", remote.get("mimeType")) if kind == "attachment" else kind
    item, _ = JiraItem.objects.update_or_create(
        source=run.source,
        kind=kind,
        remote_id=str(remote["id"]),
        defaults={"title": title, "category": category, "remote": remote},
    )
    result, _ = JiraRunItem.objects.get_or_create(
        run=run,
        item=item,
        defaults={"revision": revision(remote, run.source.user_mapping if kind in ("issue", "comment") else None)},
    )
    return result


def mark_failure(result, exc):
    logger.warning(
        "Jira item failed run=%s item=%s exception_type=%s", result.run_id, result.item_id, type(exc).__name__
    )
    result.status = "failed"
    result.error_code, result.error_message = error_details(exc)
    result.save(update_fields=["status", "error_code", "error_message", "updated_at"])


def inventory_result(run, key, title, error=None):
    result = record_remote(run, "inventory", {"id": key, "title": title})
    if error:
        mark_failure(result, error)
    else:
        result.status = "completed"
        result.save(update_fields=["status", "updated_at"])
    return result


def discover(run, client):
    sprint_fields = []
    try:
        fields = client.json("/rest/api/3/field")
        sprint_fields = [
            field["id"] for field in fields if (field.get("schema") or {}).get("custom", "").endswith(":gh-sprint")
        ]
    except Exception as exc:
        inventory_result(run, "sprint-fields", "Sprint field discovery", exc)
    try:
        for board in client.pages("/rest/agile/1.0/board", {"projectKeyOrId": run.source.remote_project_id}):
            heartbeat(run, f"Discovering sprints: {board.get('name', '')}")
            if board.get("type") != "scrum":
                continue
            for sprint in client.pages(f"/rest/agile/1.0/board/{board['id']}/sprint"):
                heartbeat(run, f"Found sprint: {sprint.get('name', '')}")
                record_remote(run, "sprint", sprint)
    except Exception as exc:
        inventory_result(run, "sprints", "Sprint / board discovery", exc)
    for issue in client.issues(run.source.remote_project_id, sprint_fields):
        if (
            str((issue["fields"].get("project") or {}).get("id", run.source.remote_project_id))
            != run.source.remote_project_id
        ):
            raise ConfluenceError("unexpected_project", "Jira returned a work item outside the selected project.")
        heartbeat(run, f"Discovering work item: {issue['key']}")
        fields = issue["fields"]
        sprints = []
        for field in sprint_fields:
            sprints.extend(value for value in (fields.get(field) or []) if isinstance(value, dict))
        issue["sprints"] = sprints
        record_remote(run, "issue", issue)
        for sprint in sprints:
            if not run.results.filter(item__kind="sprint", item__remote_id=str(sprint["id"])).exists():
                record_remote(run, "sprint", sprint)
        for attachment in fields.get("attachment") or []:
            heartbeat(run, f"Found attachment: {attachment.get('filename', '')}")
            record_remote(
                run,
                "attachment",
                {
                    **attachment,
                    "issueId": str(issue["id"]),
                    "mediaType": attachment.get("mimeType", "application/octet-stream"),
                    "fileSize": attachment.get("size", 0),
                    "downloadLink": attachment.get("content", ""),
                },
            )
        try:
            for comment in client.pages(
                f"/rest/api/3/issue/{issue['id']}/comment", {"expand": "renderedBody"}, key="comments"
            ):
                heartbeat(run, f"Found comment on {issue['key']}")
                record_remote(run, "comment", {**comment, "issueId": str(issue["id"]), "key": issue["key"]})
            # A retry of a previously failed discovery must also show a completed outcome.
            if run.source.items.filter(kind="inventory", remote_id=f"comments-{issue['id']}").exists():
                inventory_result(run, f"comments-{issue['id']}", f"Comments for {issue['key']}")
        except Exception as exc:
            inventory_result(run, f"comments-{issue['id']}", f"Comments for {issue['key']}", exc)
    for key, title in (("sprint-fields", "Sprint field discovery"), ("sprints", "Sprint / board discovery")):
        if (
            run.source.items.filter(kind="inventory", remote_id=key).exists()
            and not run.results.filter(item__kind="inventory", item__remote_id=key).exists()
        ):
            inventory_result(run, key, title)


def select_results(run, results):
    affected_issues = set()
    affected_sprints = set()
    for result in results:
        item = result.item
        if item.kind == "inventory":
            continue
        previous = item.results.exclude(run=run).exclude(status="skipped").order_by("-run__created_at").first()
        failed = bool(previous and previous.status == "failed")
        destination = getattr(
            item, {"issue": "issue", "comment": "comment", "sprint": "cycle", "attachment": "file"}[item.kind]
        )
        missing = (
            not destination or destination.deleted_at or (item.kind == "attachment" and destination.status != "active")
        )
        selected = (
            run.mode == "all"
            or (run.mode == "selected" and str(item.pk) in run.selection)
            or (run.mode == "failed" and (failed or not item.imported_revision))
            or (run.mode == "changed" and (failed or missing or item.imported_revision != result.revision))
        )
        result.status = "pending" if selected else "skipped"
        result.save(update_fields=["status", "updated_at"])
        if selected and (item.kind == "attachment" or (item.kind == "comment" and not item.comment_id)):
            affected_issues.add(item.remote["issueId"])
        if selected and item.kind == "sprint":
            affected_sprints.add(item.remote_id)
    for result in results:
        item = result.item
        if item.kind == "issue" and (
            item.remote_id in affected_issues
            or any(str(s["id"]) in affected_sprints for s in item.remote.get("sprints", []))
        ):
            if result.status == "skipped":
                result.status = "pending"
                result.save(update_fields=["status", "updated_at"])


def process_result(run, client, result):
    item = result.item
    heartbeat(run, f"Importing {item.category}: {item.title}")
    result.status = "running"
    result.save(update_fields=["status", "updated_at"])
    try:
        if item.kind == "sprint":
            save_sprint(run, item)
        elif item.kind == "attachment":
            parent = run.source.items.get(kind="issue", remote_id=item.remote["issueId"])
            if not parent.issue_id:
                raise ConfluenceError("issue_dependency_failed", "Import this attachment's work item first.")
            writable(parent.issue, run.source)
            if int(item.remote.get("fileSize", 0)) > settings.PROJECT_FILE_MAX_BYTES:
                raise ConfluenceError("file_too_large", "Attachment exceeds the Plane file-size limit.")
            stream, size = client.download(item.remote, settings.PROJECT_FILE_MAX_BYTES)
            try:
                heartbeat(run, f"Uploading file: {item.title}")
                store_attachment(
                    run, item, stream, size, link={"entity_type": "issue", "entity_id": str(parent.issue_id)}
                )
            finally:
                stream.close()
        else:
            attachments = list(run.source.items.filter(kind="attachment").select_related("file"))
            if item.kind == "issue":
                with transaction.atomic():
                    result.warning = save_issue(run, item, attachments)
                    save_relationships(run, item)
            elif item.kind == "comment":
                result.warning = save_comment(run, item, attachments)
        with transaction.atomic():
            require_active_run(run)
            item.imported_revision = result.revision
            item.save(update_fields=["imported_revision", "updated_at"])
            result.status = "completed"
            result.save(update_fields=["status", "warning", "updated_at"])
    except Exception as exc:
        mark_failure(result, exc)


@shared_task
def jira_import_task(run_id):
    if not JiraRun.objects.filter(pk=run_id, status="queued").update(status="discovering", updated_at=timezone.now()):
        return
    run = JiraRun.objects.select_related("initiated_by", "source__project__workspace").get(pk=run_id)
    try:
        heartbeat(run, "Discovering Jira work items, sprints, comments and attachments")
        config = get_jira_config(require_enabled=True)
        if config["site_url"] != run.source.site_url:
            raise ConfluenceError("site_changed", "Restore the original Jira site in God Mode to sync this source.")
        client = JiraClient(config)
        discover(run, client)
        results = list(
            run.results.select_related("item__issue", "item__comment", "item__cycle", "item__file").order_by(
                "item__kind", "item__remote_id"
            )
        )
        complete = not run.results.filter(item__kind="inventory", status="failed").exists()
        JiraRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(status="running", inventory_complete=complete)
        select_results(run, results)
        # Parents can be resolved in any source order once stable issue IDs exist.
        for result in results:
            if result.item.kind == "issue" and result.status == "pending":
                heartbeat(run, f"Preparing work item: {result.item.title}")
                try:
                    result.item.issue = prepare_issue(run, result.item)
                except Exception as exc:
                    mark_failure(result, exc)
        for kind in ("sprint", "attachment", "issue", "comment"):
            for result in results:
                if result.item.kind == kind and result.status == "pending":
                    process_result(run, client, result)
        failures = run.results.filter(status="failed").exists()
        JiraRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
            status="partial" if failures else "completed",
            phase="Finished with failures" if failures else "Finished",
            updated_at=timezone.now(),
            finished_at=timezone.now(),
        )
    except Exception as exc:
        code, message = error_details(exc)
        JiraRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
            status="failed",
            phase="Import stopped",
            error_code=code,
            error_message=message,
            updated_at=timezone.now(),
            finished_at=timezone.now(),
        )
        run.results.filter(status__in=["pending", "running"]).update(
            status="failed", error_code=code, error_message=message
        )
