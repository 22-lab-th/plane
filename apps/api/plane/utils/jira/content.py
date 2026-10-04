"""Convert Jira ADF/rendered HTML into sanitized Plane content without remote fetches."""

import base64
import hashlib
import html
import json
import os
import re
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from plane.utils.confluence.client import ConfluenceError
from plane.utils.confluence.content import rewrite_page_html


def revision(remote, mapping=None):
    # Rendered URLs can contain transient tokens; hash the source document instead.
    value = {key: val for key, val in remote.items() if key not in ("renderedFields", "renderedBody", "self")}
    return hashlib.sha256(
        json.dumps([value, mapping or {}], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def adf_html(node, site_url, issue_key):
    if node is None:
        return "<p></p>"
    if isinstance(node, str):
        return "<p>" + html.escape(node).replace("\n", "<br>") + "</p>"
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = html.escape(node.get("text", ""))
        for mark in node.get("marks", []):
            tag = {"strong": "strong", "em": "em", "strike": "s", "code": "code", "underline": "u"}.get(
                mark.get("type")
            )
            if tag:
                text = f"<{tag}>{text}</{tag}>"
            elif mark.get("type") == "link":
                href = str((mark.get("attrs") or {}).get("href", ""))
                if urlsplit(href).scheme in ("http", "https", "mailto"):
                    text = f'<a href="{html.escape(href, quote=True)}">{text}</a>'
        return text
    if kind == "mention":
        return html.escape(str(attrs.get("text", "@Jira user")))
    if kind == "emoji":
        return html.escape(str(attrs.get("text") or attrs.get("shortName", "")))
    if kind == "hardBreak":
        return "<br>"
    if kind == "rule":
        return "<hr>"
    if kind in ("inlineCard", "blockCard"):
        href = str(attrs.get("url", ""))
        if urlsplit(href).scheme in ("http", "https"):
            return f'<a href="{html.escape(href, quote=True)}">{html.escape(href)}</a>'
        return ""
    if kind == "media":
        # ADF media UUIDs are not Jira numeric attachment IDs. Rendered HTML resolves
        # them; never guess which file an image belongs to when that body is absent.
        raise ConfluenceError(
            "media_unresolved",
            f"Jira did not render embedded media for {issue_key}. Check source permissions and retry.",
        )
    content = "".join(adf_html(child, site_url, issue_key) for child in node.get("content", []))
    if kind == "taskList":
        return f'<ul data-type="taskList">{content}</ul>'
    if kind == "taskItem":
        done = str(attrs.get("state", "")).upper() == "DONE"
        box = '<input type="checkbox" checked>' if done else '<input type="checkbox">'
        return (
            f'<li data-checked="{"true" if done else "false"}" data-type="taskItem">'
            f"<label>{box}</label><div><p>{content}</p></div></li>"
        )
    tag = {
        "paragraph": "p",
        "bulletList": "ul",
        "orderedList": "ol",
        "listItem": "li",
        "blockquote": "blockquote",
        "codeBlock": "pre",
        "table": "table",
        "tableRow": "tr",
        "tableHeader": "th",
        "tableCell": "td",
    }.get(kind)
    if kind == "heading":
        level = attrs.get("level", 2)
        tag = f"h{level if isinstance(level, int) and 1 <= level <= 6 else 2}"
    return f"<{tag}>{content}</{tag}>" if tag else content


def has_task_list(node):
    """True if an ADF document contains a checklist (taskList/taskItem)."""
    if isinstance(node, dict):
        return node.get("type") in ("taskList", "taskItem") or any(has_task_list(c) for c in node.get("content", []))
    return False


def rendered_html(remote, source, attachments, *, comment=False):
    key = remote.get("key", source.project_key)
    rendered = remote.get("renderedBody") if comment else (remote.get("renderedFields") or {}).get("description")
    body = remote.get("body") if comment else remote.get("fields", {}).get("description")
    value = rendered if isinstance(rendered, str) else adf_html(body, source.site_url, key)
    if has_task_list(body):
        # Jira's rendered HTML flattens checklists into plain bullets and drops the done/not-done state
        # (every Definition of Done box would import identically). Build this body from the ADF instead.
        # Embedded media needs the rendered HTML, so such a body keeps it and loses only the checkbox state.
        try:
            value = adf_html(body, source.site_url, key)
        except ConfluenceError:
            pass
    soup = BeautifulSoup(value, "html.parser")
    host = urlsplit(source.site_url).hostname
    for node in soup.find_all(["img", "a", "video", "object"]):
        link = node.get("href" if node.name == "a" else "data" if node.name == "object" else "src", "")
        parsed = urlsplit(link)
        if parsed.hostname and parsed.hostname != host:
            continue
        match = re.search(r"/(?:secure/attachment|attachment/(?:content|thumbnail))/(\d+)(?:/|$)", parsed.path)
        if match:
            node["data-resource-id"] = match.group(1)
    return rewrite_page_html(
        str(soup),
        site_url=source.site_url,
        page_urls={},
        attachments=attachments,
        files_url=f"/{source.project.workspace.slug}/projects/{source.project_id}/files/",
    )


def convert_document(value, base_binary=b""):
    secret = os.environ.get("LIVE_SERVER_SECRET_KEY")
    if not secret:
        raise ConfluenceError("live_not_configured", "Plane Live document conversion is not configured.")
    try:
        result = requests.post(
            os.environ.get("PLANE_YJS_REPLACE_URL", "http://live:3001").rstrip("/") + "/replace-document",
            json={"description_html": value, "base_binary": base64.b64encode(base_binary or b"").decode()},
            headers={"live-server-secret-key": secret},
            timeout=30,
        )
        result.raise_for_status()
        data = result.json()
        binary = base64.b64decode(data["description_binary"], validate=True)
        if (
            not binary
            or not isinstance(data["description_json"], dict)
            or not isinstance(data["description_html"], str)
        ):
            raise ValueError("Invalid conversion")
        return data["description_html"], data["description_json"], binary
    except (requests.RequestException, ValueError, KeyError):
        raise ConfluenceError(
            "live_conversion_failed", "Plane Live could not convert the Jira content. Check Live and retry."
        ) from None


def choose_sprint(sprints):
    """Plane has one current cycle per issue; prefer active, then future, then latest closed."""
    values = [s for s in sprints if isinstance(s, dict) and str(s.get("id", "")).isdigit()]
    return max(
        values,
        key=lambda s: ({"active": 3, "future": 2, "closed": 1}.get(s.get("state"), 0), int(s["id"])),
        default=None,
    )
