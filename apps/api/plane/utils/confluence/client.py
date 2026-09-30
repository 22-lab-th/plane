"""Confluence Cloud API with bounded reads, pagination and credential-safe redirects."""

import base64
import re
import time
from tempfile import SpooledTemporaryFile
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from plane.utils.url_security import pinned_fetch


class ConfluenceError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def validate_site_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
    except ValueError:
        raise ConfluenceError("invalid_site", "Use https://your-site.atlassian.net (optionally /wiki).") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9-]+\.atlassian\.net", parsed.hostname)
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.path.rstrip("/") not in ("", "/wiki")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfluenceError("invalid_site", "Use https://your-site.atlassian.net (optionally /wiki).")
    return f"https://{parsed.hostname}"


def category_for(kind, mime):
    if kind == "page":
        return "page"
    mime = (mime or "application/octet-stream").split(";")[0].lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return mime  # Keep PDF/Word/Excel/etc. as separate document types.


def should_import(mode, *, imported_version, remote_version, failed=False, selected=False, destination_exists=True):
    if mode == "all":
        return True
    if mode == "selected":
        return selected
    if mode == "failed":
        return failed or imported_version == 0
    return failed or not destination_exists or imported_version != remote_version


class ConfluenceClient:
    def __init__(self, config):
        self.site = validate_site_url(config["site_url"])
        self.email = config["email"]
        self.token = config["api_token"]
        cloud_id = config.get("cloud_id", "")
        if cloud_id and not re.fullmatch(r"[a-fA-F0-9-]{36}", cloud_id):
            raise ConfluenceError("invalid_cloud_id", "Enter the Atlassian Cloud ID UUID.")
        self.api = f"https://api.atlassian.com/ex/confluence/{cloud_id}" if cloud_id else self.site
        self.authorization = "Basic " + base64.b64encode(f"{self.email}:{self.token}".encode()).decode()

    def api_url(self, path):
        if path.startswith(self.site + "/wiki/"):
            path = path[len(self.site) :]
        if path.startswith("/wiki/"):
            return self.api + path
        absolute = urljoin(self.api + "/wiki/", path)
        if not absolute.startswith(self.api + "/wiki/"):
            raise ConfluenceError("unsafe_api_link", "Confluence returned an unexpected API pagination URL.")
        return absolute

    def request(self, url, *, download=False):
        initial_host = urlsplit(url).hostname
        if initial_host not in (urlsplit(self.site).hostname, "api.atlassian.com"):
            raise ConfluenceError(
                "unsafe_download", "Attachment download must originate from the configured Atlassian site."
            )
        for redirect in range(6):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ConfluenceError("unsafe_download", "Confluence returned an unsafe download redirect.")
            headers = {"Accept": "application/octet-stream" if download else "application/json"}
            if parsed.hostname == initial_host:
                headers["Authorization"] = self.authorization
            for attempt in range(3):
                try:
                    response = pinned_fetch("GET", url, headers=headers, timeout=(10, 30), stream=True)
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise ConfluenceError(
                            "network_error", "Cannot reach Atlassian: network, DNS or TLS connection failed."
                        ) from None
                    time.sleep(attempt + 1)
                    continue
                if response.status_code == 429 or response.status_code in (502, 503, 504):
                    if attempt < 2:
                        try:
                            delay = min(max(float(response.headers.get("Retry-After", 2)), 1), 30)
                        except ValueError:
                            delay = 2
                        response.close()
                        time.sleep(delay)
                        continue
                break
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not download or not location:
                    raise ConfluenceError(
                        "unexpected_redirect",
                        "Atlassian redirected an API request; check the site and token configuration.",
                    )
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                code = response.status_code
                response.close()
                reasons = {
                    401: "Atlassian rejected the API token or email; check token validity and expiry in God Mode.",
                    403: "The Atlassian account/token does not have permission or the required API scope.",
                    404: "The Confluence item was removed or is not visible to this account.",
                    429: "Atlassian rate limit reached after three attempts; retry later.",
                }
                raise ConfluenceError(f"atlassian_http_{code}", reasons.get(code, f"Atlassian returned HTTP {code}."))
            return response
        raise ConfluenceError("redirect_limit", "Attachment exceeded the download redirect limit.")

    def json(self, path, params=None):
        url = self.api_url(path)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        response = self.request(url)
        started = time.monotonic()
        try:
            raw = bytearray()
            for chunk in response.iter_content(65536):
                if time.monotonic() - started > 180:
                    raise ConfluenceError("response_timeout", "Atlassian JSON response exceeded three minutes.")
                raw.extend(chunk)
                if len(raw) > 10 * 1024 * 1024:
                    raise ConfluenceError("response_too_large", "Atlassian JSON response exceeds 10 MB.")
            import json

            return json.loads(raw)
        except (ValueError, requests.RequestException):
            raise ConfluenceError("invalid_response", "Atlassian returned incomplete or invalid JSON.") from None
        finally:
            response.close()

    def paginate(self, path, params=None):
        visited = set()
        for _ in range(10000):
            if path in visited:
                raise ConfluenceError("pagination_loop", "Atlassian returned a repeated pagination cursor.")
            visited.add(path)
            payload = self.json(path, params)
            for result in payload.get("results", []):
                yield result
            path = payload.get("_links", {}).get("next")
            params = None
            if not path:
                return
        raise ConfluenceError("pagination_limit", "The space exceeds the supported pagination limit.")

    def download(self, attachment, max_bytes):
        link = attachment.get("downloadLink") or attachment.get("_links", {}).get("download")
        if not link:
            raise ConfluenceError("missing_download", "Atlassian returned no attachment download link.")
        url = urljoin(self.site + "/wiki/", link)
        if self.api != self.site and url.startswith(self.site + "/wiki/"):
            url = self.api + url[len(self.site) :]
        response = self.request(url, download=True)
        output = SpooledTemporaryFile(max_size=5 * 1024 * 1024)
        started = time.monotonic()
        size = 0
        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ConfluenceError("file_too_large", "Attachment exceeds the Plane file-size limit.")
                if time.monotonic() - started > 180:
                    raise ConfluenceError("download_timeout", "Attachment download exceeded three minutes.")
                output.write(chunk)
            if size != int(attachment.get("fileSize", size)):
                raise ConfluenceError(
                    "size_mismatch",
                    "Downloaded size differs from Atlassian metadata. Retry after the source update finishes.",
                )
            output.seek(0)
            return output, size
        except Exception:
            output.close()
            raise
        finally:
            response.close()
