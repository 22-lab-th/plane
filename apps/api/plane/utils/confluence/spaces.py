"""Search the complete accessible space directory, including later API pages."""

import hashlib
import json
import re

from django.core.cache import cache

from plane.utils.confluence.client import ConfluenceError


def search_spaces(client, query, cursor=""):
    if cursor and not re.fullmatch(r"[0-9]{1,9}", cursor):
        raise ConfluenceError("invalid_cursor", "Invalid search cursor.")
    offset = int(cursor or 0)
    # Isolate the short-lived directory by connection and token, including rotations.
    identity = json.dumps([client.site, client.api, client.email, client.authorization])
    key = "confluence:space-directory:" + hashlib.sha256(identity.encode()).hexdigest()
    spaces = cache.get(key)
    if spaces is None:
        spaces = list(
            {
                str(row["id"]): {"id": str(row["id"]), "name": row["name"], "key": row["key"]}
                for row in client.paginate("/wiki/api/v2/spaces", {"limit": 100})
            }.values()
        )
        # Cache only a complete successful read; never turn a failed page into "no matches".
        cache.set(key, spaces, timeout=60)
    term = query.casefold()
    matches = [row for row in spaces if term in row["name"].casefold() or term in row["key"].casefold()]
    matches.sort(
        key=lambda row: (term not in (row["name"].casefold(), row["key"].casefold()), row["name"].casefold(), row["id"])
    )
    return {
        "results": matches[offset : offset + 100],
        "next_cursor": str(offset + 100) if offset + 100 < len(matches) else None,
        "count": len(matches),
    }
