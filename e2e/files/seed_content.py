"""Create the folder tree and a handful of files through the real API.

Runs on the **host** (the API and the published MinIO port are both reachable as
127.0.0.1 there, while inside the API container the presigned URL's host is not).
Uses only the standard library so the harness needs no host-side packages.

Creates two folders (one nested), uploads files into the root and into each folder, and
leaves one file in the trash, so every quick view has rows the product itself produced.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

API_URL = os.environ["E2E_API_URL"]
WEB_URL = os.environ["E2E_WEB_URL"]
SLUG = os.environ["E2E_WORKSPACE_SLUG"]
PROJECT_ID = os.environ["E2E_PROJECT_ID"]
EMAIL = os.environ["E2E_EMAIL"]
PASSWORD = os.environ["E2E_PASSWORD"]

PDF = b"%PDF-1.7\nseed payload\n%%EOF\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect()
    )


def request(opener, method, url, *, headers=None, json_data=None, form_data=None, timeout=60):
    """One request through the session opener; returns (status, parsed json or None)."""
    body = None
    if json_data is not None:
        body = json.dumps(json_data).encode()
    elif form_data is not None:
        body = urllib.parse.urlencode(form_data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if json_data is not None:
        req.add_header("Content-Type", "application/json")
    if form_data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with opener.open(req, timeout=timeout) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        if error.code < 400:
            # The sign-in endpoint answers 302 and this opener never follows redirects.
            return error.code, None
        raise AssertionError(f"{method} {url} -> {error.code}: {error.read()[:400]!r}") from error


def main():
    opener = build_opener()
    _, csrf = request(opener, "GET", f"{API_URL}/auth/get-csrf-token/")
    status, _ = request(
        opener,
        "POST",
        f"{API_URL}/auth/sign-in/",
        headers={"X-CSRFToken": csrf["csrf_token"]},
        form_data={"email": EMAIL, "password": PASSWORD},
    )
    assert status in (200, 301, 302), status

    headers = {"X-CSRFToken": csrf["csrf_token"], "Origin": WEB_URL, "Referer": f"{WEB_URL}/"}
    files_url = f"{API_URL}/api/workspaces/{SLUG}/projects/{PROJECT_ID}/files"
    folders_url = f"{files_url}/folders/"

    _, outer = request(opener, "POST", folders_url, headers=headers, json_data={"name": "Design"})
    outer_id = outer["folder"]["id"]
    _, inner = request(
        opener, "POST", folders_url, headers=headers, json_data={"name": "Contracts", "parent_id": outer_id}
    )
    inner_id = inner["folder"]["id"]

    for name, folder_id in (
        ("roadmap.pdf", None),
        ("budget.pdf", None),
        ("brief.pdf", outer_id),
        ("offer.pdf", inner_id),
    ):
        payload = {"file_name": name, "size_bytes": len(PDF), "mime_type": "application/pdf"}
        if folder_id:
            payload["folder_id"] = folder_id
        _, body = request(opener, "POST", f"{files_url}/initiate-upload/", headers=headers, json_data=payload)
        upload = body["upload"]
        put = urllib.request.Request(upload["url"], data=PDF, method="PUT")
        for key, value in upload["headers"].items():
            put.add_header(key, value)
        with urllib.request.urlopen(put, timeout=60) as response:
            assert response.status == 200, response.status
        request(
            opener,
            "POST",
            f"{files_url}/{body['file']['id']}/complete-upload/",
            headers=headers,
            json_data={"version_no": body["version_no"], "size_bytes": len(PDF)},
        )

    _, trashed = request(
        opener,
        "POST",
        f"{files_url}/initiate-upload/",
        headers=headers,
        json_data={"file_name": "old-draft.pdf", "size_bytes": len(PDF), "mime_type": "application/pdf"},
    )
    put = urllib.request.Request(trashed["upload"]["url"], data=PDF, method="PUT")
    for key, value in trashed["upload"]["headers"].items():
        put.add_header(key, value)
    with urllib.request.urlopen(put, timeout=60) as response:
        assert response.status == 200, response.status
    request(
        opener,
        "POST",
        f"{files_url}/{trashed['file']['id']}/complete-upload/",
        headers=headers,
        json_data={"version_no": trashed["version_no"], "size_bytes": len(PDF)},
    )
    status, _ = request(opener, "DELETE", f"{files_url}/{trashed['file']['id']}/", headers=headers)
    assert status == 204, status

    print("SEEDED folders=%s nested=%s" % (outer_id, inner_id), file=sys.stderr)


if __name__ == "__main__":
    main()
