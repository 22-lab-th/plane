"""Confluence imports run outside HTTP requests and persist every item outcome."""

import logging
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from plane.db.models import ConfluenceItem, ConfluenceRun, ConfluenceRunItem, ConfluenceSource, Page, ProjectPage
from plane.utils.confluence.client import ConfluenceClient, ConfluenceError, category_for, should_import
from plane.utils.confluence.config import get_confluence_config
from plane.utils.confluence.content import rewrite_page_html
from plane.utils.confluence.documents import replace_imported_page
from plane.utils.confluence.jobs import ACTIVE, error_details, require_import_permission, require_active_run
from plane.utils.confluence.storage import store_attachment

logger = logging.getLogger(__name__)


def heartbeat(run, phase):
    if not ConfluenceRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(phase=phase, updated_at=timezone.now()):
        raise ConfluenceError("run_interrupted", "This run has been interrupted; start another run to continue.")
    require_import_permission(run.initiated_by, run.source.project, run.source)


def record_remote(run, kind, remote):
    remote_id = str(remote["id"])
    item, _ = ConfluenceItem.objects.update_or_create(
        source=run.source,
        kind=kind,
        remote_id=remote_id,
        defaults={
            "title": remote.get("title", ""),
            "category": category_for(kind, remote.get("mediaType")),
            "remote": remote,
        },
    )
    result, _ = ConfluenceRunItem.objects.get_or_create(
        run=run, item=item, defaults={"version": int(remote.get("version", {}).get("number", 1))}
    )
    return result


def mark_failure(result, exc):
    logger.warning(
        "Confluence item failed run=%s item=%s exception_type=%s", result.run_id, result.item_id, type(exc).__name__
    )
    result.status = "failed"
    result.error_code, result.error_message = error_details(exc)
    result.save(update_fields=["status", "error_code", "error_message", "updated_at"])


def make_node(source, title, parent, node_type, remote_id=None):
    node = Page.objects.create(
        workspace=source.project.workspace,
        owned_by=source.owner,
        created_by=source.owner,
        updated_by=source.owner,
        name=title,
        parent=parent,
        access=source.access,
        node_type=node_type,
        external_source="confluence",
        external_id=remote_id,
    )
    ProjectPage.objects.create(
        workspace=source.project.workspace,
        project=source.project,
        page=node,
        created_by=source.owner,
        updated_by=source.owner,
    )
    return node


def writable_node(source, node):
    if (
        node.deleted_at
        or node.archived_at
        or node.is_locked
        or not ProjectPage.objects.filter(page=node, project_id=source.project_id).exists()
    ):
        raise ConfluenceError(
            "destination_not_writable",
            "The destination was removed, moved, archived or locked. Restore/unlock it before retrying.",
        )
    if node.access != source.access or (node.access == Page.PRIVATE_ACCESS and node.owned_by_id != source.owner_id):
        raise ConfluenceError(
            "destination_access_changed",
            "The destination access or private owner changed. Restore its original access before syncing.",
        )


def ensure_destinations(run, results):
    source = run.source
    # Keep creation and mapping atomic: a retry never leaves an unmapped duplicate.
    with transaction.atomic():
        locked = ConfluenceSource.objects.select_for_update().get(pk=source.pk)
        if locked.root_id:
            writable_node(source, locked.root)
            if locked.root.node_type != Page.FOLDER_NODE:
                raise ConfluenceError("destination_not_folder", "The import root must remain a folder.")
        else:
            if source.parent_id:
                writable_node(source, source.parent)
                if source.parent.node_type != Page.FOLDER_NODE:
                    raise ConfluenceError("destination_not_folder", "The destination parent must remain a folder.")
            locked.root = make_node(source, source.space_name or source.space_id, source.parent, Page.FOLDER_NODE)
            locked.save(update_fields=["root"])
        source.root = locked.root
    pages = {r.item.remote_id: r for r in results if r.item.kind == "page"}
    children = {str(r.item.remote.get("parentId", "")) for r in pages.values()}
    visiting = set()
    prepared = {}

    def ensure(remote_id):
        if remote_id in prepared:
            return prepared[remote_id]
        heartbeat(run, f"Preparing page: {pages[remote_id].item.title}")
        result = pages[remote_id]
        item = result.item
        if remote_id in visiting:
            raise ConfluenceError("invalid_hierarchy", "Confluence returned a cyclic page hierarchy.")
        visiting.add(remote_id)
        parent_id = str(item.remote.get("parentId", ""))
        parent = source.root
        if parent_id in pages:
            parent_item = ensure(parent_id)
            parent = parent_item.folder
        visiting.remove(remote_id)
        with transaction.atomic():
            current = ConfluenceItem.objects.select_for_update().get(pk=item.pk)
            if remote_id in children:
                if current.folder_id:
                    writable_node(source, current.folder)
                    if current.folder.node_type != Page.FOLDER_NODE:
                        raise ConfluenceError(
                            "destination_not_folder", "An imported hierarchy folder was converted to a page."
                        )
                    Page.objects.filter(pk=current.folder_id).update(name=item.title, parent=parent)
                else:
                    current.folder = make_node(source, item.title, parent, Page.FOLDER_NODE)
            page_parent = current.folder if current.folder_id else parent
            if current.page_id:
                writable_node(source, current.page)
                if current.page.node_type != Page.PAGE_NODE:
                    raise ConfluenceError("destination_not_page", "An imported page was converted to a folder.")
                Page.objects.filter(pk=current.page_id).update(
                    parent=page_parent, sort_order=item.remote.get("position") or Page.DEFAULT_SORT_ORDER
                )
            else:
                current.page = make_node(source, item.title, page_parent, Page.PAGE_NODE, item.remote_id)
            current.save(update_fields=["page", "folder", "updated_at"])
            item.page, item.folder = current.page, current.folder
        prepared[remote_id] = item
        return item

    # Only create selected pages and their ancestors, but check shared destinations as needed.
    for result in results:
        if result.item.kind == "page" and result.status == "pending":
            try:
                ensure(result.item.remote_id)
            except Exception as exc:
                mark_failure(result, exc)
            finally:
                visiting.clear()


def select_results(run, results):
    affected_pages = set()
    for result in results:
        item = result.item
        previous = item.results.exclude(run=run).exclude(status="skipped").order_by("-run__created_at").first()
        failed = bool(previous and previous.status == "failed")
        destination = item.page if item.kind == "page" else item.file
        exists = bool(
            destination and not destination.deleted_at and (item.kind == "page" or destination.status == "active")
        )
        selected = should_import(
            run.mode,
            imported_version=item.imported_version,
            remote_version=result.version,
            failed=failed,
            selected=str(item.pk) in run.selection,
            destination_exists=exists,
        )
        if result.status == "failed":
            continue  # Attachment inventory errors must be visible, never counted as unchanged.
        result.status = "pending" if selected else "skipped"
        result.save(update_fields=["status", "updated_at"])
        if selected and item.kind == "attachment":
            affected_pages.add(str(item.remote.get("pageId", "")))
    # Rebuild owner pages after media retries/updates so failed references recover in the same run.
    for result in results:
        if result.item.kind == "page" and result.item.remote_id in affected_pages and result.status == "skipped":
            result.status = "pending"
            result.save(update_fields=["status", "updated_at"])


def process_result(run, client, result, page_urls):
    item = result.item
    heartbeat(run, f"Importing {item.category}: {item.title}")
    result.status = "running"
    result.save(update_fields=["status", "updated_at"])
    try:
        if item.kind == "attachment":
            if int(item.remote.get("fileSize", 0)) > settings.PROJECT_FILE_MAX_BYTES:
                raise ConfluenceError("file_too_large", "Attachment exceeds the Plane file-size limit.")
            stream, size = client.download(item.remote, settings.PROJECT_FILE_MAX_BYTES)
            try:
                heartbeat(run, f"Uploading file: {item.title}")
                store_attachment(run, item, stream, size)
            finally:
                stream.close()
        else:
            remote = client.json(f"/wiki/api/v2/pages/{item.remote_id}", {"body-format": "view"})
            if int(remote.get("version", {}).get("number", 0)) != result.version:
                raise ConfluenceError(
                    "source_changed",
                    "The page changed during this import. Sync changed items again to use a consistent version.",
                )
            html = remote.get("body", {}).get("view", {}).get("value")
            if html is None:
                raise ConfluenceError("missing_body", "Atlassian returned no rendered page body.")
            attachments = run.source.items.filter(kind="attachment").select_related("file")
            html = rewrite_page_html(
                html,
                site_url=run.source.site_url,
                page_urls=page_urls,
                attachments=attachments,
                files_url=f"/{run.source.project.workspace.slug}/projects/{run.source.project_id}/files",
            )
            heartbeat(run, f"Saving page: {item.title}")
            replace_imported_page(run, item, html)
        require_active_run(run)
        item.imported_version = result.version
        item.save(update_fields=["imported_version", "updated_at"])
        result.status = "completed"
        result.save(update_fields=["status", "updated_at"])
    except Exception as exc:
        mark_failure(result, exc)


@shared_task
def confluence_import_task(run_id):
    if not ConfluenceRun.objects.filter(pk=run_id, status="queued").update(
        status="discovering", updated_at=timezone.now()
    ):
        return
    run = ConfluenceRun.objects.select_related(
        "initiated_by", "source__project__workspace", "source__owner", "source__parent"
    ).get(pk=run_id)
    try:
        heartbeat(run, "Discovering pages and attachments")
        config = get_confluence_config(require_enabled=True)
        if config["site_url"] != run.source.site_url:
            raise ConfluenceError(
                "site_changed", "The configured Atlassian site changed. Restore the original site to sync this source."
            )
        client = ConfluenceClient(config)
        for page in client.paginate(f"/wiki/api/v2/spaces/{run.source.space_id}/pages", {"limit": 100}):
            heartbeat(run, f"Discovering attachments: {page.get('title', '')}")
            page_result = record_remote(run, "page", page)
            try:
                for attachment in client.paginate(f"/wiki/api/v2/pages/{page['id']}/attachments", {"limit": 100}):
                    heartbeat(run, f"Found attachment: {attachment.get('title', '')}")
                    record_remote(run, "attachment", {**attachment, "pageId": str(page["id"])})
            except Exception as exc:
                mark_failure(page_result, ConfluenceError("attachment_inventory_failed", error_details(exc)[1]))
        results = list(
            run.results.select_related("item__page", "item__folder", "item__file").order_by(
                "item__kind", "item__remote_id"
            )
        )
        run.inventory_complete = not any(r.error_code == "attachment_inventory_failed" for r in results)
        ConfluenceRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
            status="running", inventory_complete=run.inventory_complete
        )
        select_results(run, results)
        heartbeat(run, "Preparing destination pages")
        ensure_destinations(run, results)
        pages = run.source.items.filter(kind="page", page__isnull=False)
        page_urls = {
            p.remote_id: f"/{run.source.project.workspace.slug}/projects/{run.source.project_id}/pages/{p.page_id}"
            for p in pages
        }
        for kind in ("attachment", "page"):
            for result in results:
                if result.item.kind == kind and result.status == "pending":
                    process_result(run, client, result, page_urls)
        failures = run.results.filter(status="failed").exists()
        ConfluenceRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
            status="partial" if failures else "completed",
            phase="Finished with failures" if failures else "Finished",
            updated_at=timezone.now(),
            finished_at=timezone.now(),
        )
    except Exception as exc:
        code, message = error_details(exc)
        ConfluenceRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
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
