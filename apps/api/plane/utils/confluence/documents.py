import base64
import binascii
import os
import requests
from django.db import transaction
from plane.db.models import Page, PageVersion, ProjectPage
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.jobs import require_active_run


def replace_imported_page(run, item, html):
    """Replace all editor representations, preserving Yjs deletion tombstones."""
    secret = os.environ.get("LIVE_SERVER_SECRET_KEY")
    if not secret:
        raise ConfluenceError("live_not_configured", "Plane Live document replacement is not configured.")
    with transaction.atomic():
        page = Page.objects.select_for_update().get(pk=item.page_id, projects=run.source.project_id)
        if not ProjectPage.objects.filter(page=page, project_id=run.source.project_id).exists():
            raise ConfluenceError("destination_moved", "The imported page no longer belongs to this project.")
        if page.node_type != Page.PAGE_NODE or page.access != run.source.access:
            raise ConfluenceError(
                "destination_access_changed", "The imported page type or access changed. Restore it before syncing."
            )
        if page.is_locked or page.archived_at:
            raise ConfluenceError(
                "page_not_writable", "The destination page is locked or archived. Unlock/unarchive it before retrying."
            )
        if page.access == Page.PRIVATE_ACCESS and page.owned_by_id != run.initiated_by_id:
            raise ConfluenceError("page_permission", "Only the owner may replace this private page.")
        try:
            result = requests.post(
                os.environ.get("PLANE_YJS_REPLACE_URL", "http://live:3001").rstrip("/") + "/replace-document",
                json={
                    "base_binary": base64.b64encode(page.description_binary or b"").decode(),
                    "description_html": html,
                },
                headers={"live-server-secret-key": secret},
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            binary = base64.b64decode(data["description_binary"], validate=True)
            content_json = data["description_json"]
            content_html = data["description_html"]
            if not binary or not isinstance(content_json, dict) or not isinstance(content_html, str):
                raise ValueError("Invalid editor conversion")
        except (requests.RequestException, ValueError, KeyError, binascii.Error):
            raise ConfluenceError(
                "live_conversion_failed", "Plane Live could not convert the page. Verify the Live service and retry."
            ) from None
        require_active_run(run)
        PageVersion.objects.create(
            workspace=page.workspace,
            page=page,
            owned_by=run.initiated_by,
            description_html=page.description_html,
            description_json=page.description_json,
            description_binary=page.description_binary,
        )
        page.name = item.title
        page.description_html = content_html
        page.description_json = content_json
        page.description_binary = binary
        page.save(
            update_fields=[
                "name",
                "description_html",
                "description_json",
                "description_binary",
                "description_stripped",
                "updated_at",
            ]
        )
