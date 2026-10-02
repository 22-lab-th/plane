"""Jira Cloud REST v3 and Agile APIs over the shared DNS-pinned transport."""

import re
from urllib.parse import urlsplit
from plane.utils.confluence.client import ConfluenceClient, ConfluenceError


class JiraClient(ConfluenceClient):
    def __init__(self, config):
        super().__init__(config, product="jira")

    def api_url(self, path):
        if path.startswith(self.site + "/rest/"):
            path = path[len(self.site) :]
        if not path.startswith(("/rest/api/3/", "/rest/agile/1.0/")) or ".." in urlsplit(path).path:
            raise ConfluenceError("unsafe_api_link", "Jira returned an unexpected API URL.")
        return self.api + path

    def download_url(self, attachment):
        remote_id = str(attachment["id"])
        if not re.fullmatch(r"[0-9]+", remote_id):
            raise ConfluenceError("invalid_attachment", "Jira returned an invalid attachment ID.")
        return self.api_url(f"/rest/api/3/attachment/content/{remote_id}")

    def pages(self, path, params=None, *, key="values"):
        """Classic startAt pagination for projects, boards, sprints and comments."""
        params = {**(params or {}), "maxResults": 100, "startAt": 0}
        for _ in range(10000):
            data = self.json(path, params)
            rows = data.get(key, [])
            yield from rows
            start = int(data.get("startAt", params["startAt"])) + len(rows)
            if data.get("isLast") or ("total" in data and start >= int(data["total"])):
                return
            if not rows and "total" not in data and data.get("isLast") is not False:
                return
            if not rows or start <= params["startAt"]:
                raise ConfluenceError("pagination_loop", "Jira pagination did not advance.")
            params["startAt"] = start
        raise ConfluenceError("pagination_limit", "Jira exceeds the supported pagination limit.")

    def issues(self, project_id, sprint_fields):
        if not re.fullmatch(r"[0-9]+", str(project_id)):
            raise ConfluenceError("invalid_project", "Choose a valid Jira project.")
        params = {
            "jql": f"project = {project_id} ORDER BY id ASC",
            "maxResults": 100,
            "fields": ",".join(
                [
                    "summary",
                    "description",
                    "status",
                    "priority",
                    "labels",
                    "assignee",
                    "reporter",
                    "project",
                    "parent",
                    "issuetype",
                    "duedate",
                    "updated",
                    "attachment",
                    "issuelinks",
                    *sprint_fields,
                ]
            ),
            "expand": "renderedFields",
        }
        seen = set()
        for _ in range(10000):
            data = self.json("/rest/api/3/search/jql", params)
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast"):
                return
            if not token:
                if data.get("isLast") is False:
                    raise ConfluenceError("pagination_incomplete", "Jira omitted the next search cursor.")
                return
            if token in seen:
                raise ConfluenceError("pagination_loop", "Jira returned a repeated search cursor.")
            seen.add(token)
            params["nextPageToken"] = token
        raise ConfluenceError("pagination_limit", "Jira exceeds the supported search limit.")
