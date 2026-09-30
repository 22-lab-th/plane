"""Convert rendered Confluence HTML to Plane's durable media nodes in document order."""

from urllib.parse import unquote, urlsplit, parse_qs
from bs4 import BeautifulSoup
from plane.utils.content_validator import validate_html_content
from plane.utils.confluence.client import ConfluenceError

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/ogg"}


def attachment_path(link):
    return unquote(urlsplit(link).path).removeprefix("/wiki").replace("/download/thumbnails/", "/download/attachments/")


def rewrite_page_html(html, *, site_url, page_urls, attachments, files_url):
    soup = BeautifulSoup(html, "html.parser")
    by_id = {}
    by_path = {}
    for item in attachments:
        by_id[item.remote_id] = item
        by_id[item.remote_id.removeprefix("att")] = item
        link = item.remote.get("downloadLink") or item.remote.get("_links", {}).get("download", "")
        by_path[attachment_path(link)] = item
    missing = set()
    for node in list(soup.find_all(["img", "video", "audio", "object", "embed", "a"])):
        if not node.parent:
            continue
        if node.name == "a" and node.find(["img", "video", "audio", "object", "embed"]):
            continue
        src = node.get("href" if node.name == "a" else "data" if node.name == "object" else "src", "")
        if node.name == "img" and node.get("data-image-src"):
            src = node["data-image-src"]
        if not src and node.name in ("video", "audio"):
            child = node.find("source")
            src = child.get("src", "") if child else ""
        if not src and node.name == "object":
            child = node.find("param", attrs={"name": "movie"})
            src = child.get("value", "") if child else ""
        path = attachment_path(src)
        remote_id = str(node.get("data-linked-resource-id") or node.get("data-resource-id") or "")
        item = by_id.get(remote_id) or by_path.get(path)
        if item:
            file = item.file
            if not file or file.deleted_at or file.status != "active":
                missing.add(item.title)
                continue
            mime = file.mime_type
            tag = (
                "image-component"
                if node.name == "img" and mime in IMAGE_TYPES
                else (
                    "video-component"
                    if node.name in ("img", "video", "object", "embed", "a") and mime in VIDEO_TYPES
                    else "a"
                )
            )
            replacement = soup.new_tag(tag)
            if tag == "a":
                replacement["href"] = f"{files_url}?file={file.id}"
                replacement.string = node.get_text(strip=True) or item.title
            else:
                replacement["src"] = f"project-file:{file.id}"
                replacement["status"] = "uploaded"
                replacement["alignment"] = "center"
                for attribute in ("width", "height"):
                    value = str(node.get(attribute, ""))
                    if value.removesuffix("px").isdigit():
                        replacement[attribute] = value
            node.replace_with(replacement)
        elif node.name == "a":
            parsed = urlsplit(src)
            source_page_id = (parse_qs(parsed.query).get("pageId") or [""])[0]
            if not source_page_id and "/pages/" in parsed.path:
                source_page_id = parsed.path.split("/pages/", 1)[1].split("/", 1)[0]
            if source_page_id in page_urls and (not parsed.hostname or parsed.hostname == urlsplit(site_url).hostname):
                node["href"] = page_urls[source_page_id] + (f"#{parsed.fragment}" if parsed.fragment else "")
        else:
            parsed = urlsplit(src)
            missing.add((parsed.hostname or "") + parsed.path if src else "Media with no download source")
    if missing:
        raise ConfluenceError(
            "media_dependency_failed", "Page media not available in Plane Files: " + "; ".join(sorted(missing))[:1500]
        )
    valid, error, cleaned = validate_html_content(str(soup))
    if not valid:
        raise ConfluenceError("invalid_content", error)
    return cleaned or "<p></p>"
