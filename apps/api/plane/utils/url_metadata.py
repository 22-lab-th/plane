# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from plane.utils.url_security import pinned_fetch_following_redirects


MAX_HTML_BYTES = 1_000_000
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = (3, 5)
USER_AGENT = "Plane-Bookmark-Preview/1.0 (+https://plane.so)"


class URLMetadataError(ValueError):
    pass


def _clean_text(value, max_length):
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned[:max_length] or None


def _meta_content(soup, selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return tag["content"]
    return None


def fetch_url_metadata(url):
    """Fetch title and description from a public HTML page using the SSRF-safe client."""
    normalized_url = url.strip()
    if not normalized_url.startswith(("http://", "https://")):
        normalized_url = f"https://{normalized_url}"
    if len(normalized_url) > 2048:
        raise URLMetadataError("The URL is too long to preview.")
    parsed_url = urlsplit(normalized_url)
    if parsed_url.username is not None or parsed_url.password is not None:
        raise URLMetadataError("URLs containing credentials cannot be previewed.")

    response = None
    try:
        response, final_url = pinned_fetch_following_redirects(
            "GET",
            normalized_url,
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            max_redirects=MAX_REDIRECTS,
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            raise URLMetadataError("The URL did not return an HTML page.")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                parsed_content_length = int(content_length)
            except ValueError as exc:
                raise URLMetadataError("The server returned an invalid content length.") from exc
            if parsed_content_length > MAX_HTML_BYTES:
                raise URLMetadataError("The HTML page is too large to preview.")

        chunks = []
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > MAX_HTML_BYTES:
                raise URLMetadataError("The HTML page is too large to preview.")
            chunks.append(chunk)

        soup = BeautifulSoup(b"".join(chunks), "html.parser")
        title = _meta_content(soup, ['meta[property="og:title"]', 'meta[name="twitter:title"]'])
        if not title and soup.title:
            title = soup.title.get_text()
        description = _meta_content(
            soup,
            [
                'meta[name="description"]',
                'meta[property="og:description"]',
                'meta[name="twitter:description"]',
            ],
        )

        return {
            "url": final_url,
            "title": _clean_text(title, 255),
            "description": _clean_text(description, 1000),
        }
    finally:
        if response is not None:
            response.close()
