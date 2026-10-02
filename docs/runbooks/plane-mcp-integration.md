# Plane MCP integration

Use the [22lab Plane MCP server](https://github.com/22-lab-th/plane-mcp-server)
with this distribution. Its [feature guide](https://github.com/22-lab-th/plane-mcp-server/blob/main/docs/22lab-features.md)
documents installation, tools, imports, files, versions and recovery. The public
extension routes and MCP implementation must be upgraded together.

## Public token API

The following operations reuse the existing app handlers under `/api/v1`:

| Route under `/api/v1`                                                              | Purpose                                                                                       |
| ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `workspaces/{slug}/projects/{project}/files/...`                                   | Full Project Files lifecycle, folders, delivery, versions, entity links, activity and storage |
| `workspaces/{slug}/projects/{project}/confluence/...`                              | Search spaces, start/list imports and retrieve progress/results                               |
| `workspaces/{slug}/projects/{project}/jira/...`                                    | List projects/members, start/list imports and retrieve progress/results                       |
| `workspaces/{slug}/projects/{project}/pages/...`                                   | Additional lock, access, move, versions, duplicate and explicit `delete/` operations          |
| `workspaces/{slug}/projects/{project}/pages-summary/` and `favorite-pages/{page}/` | Summary and favorites                                                                         |
| `workspaces/{slug}/bookmarks/...` and `bookmark-groups/...`                        | Shared workspace bookmark/group CRUD and metadata                                             |
| `instance/confluence/`, `instance/jira/` and each connector's `test/`              | Redacted connector configuration and saved connection tests                                   |

Existing public Page CRUD, archive and hierarchy routes remain authoritative for
HTML/editor conversion. Deletion uses `pages/{page}/delete/` to preserve existing
public detail-route behavior. Page content writes require the Live conversion
service. Import jobs require the worker/broker. No database migrations are added
by this token surface itself.

Authenticate with `X-Api-Key: <Plane API token>`. These routes accept no session
fallback. The token must be active and unexpired, its owner active, its workspace
binding must match, and the owner must be an active workspace member. Original
project/file/private-page permissions, archived-project behavior, quotas, upload
validation, activity logging and operation-specific throttles remain in force.
API-token rate limits also apply.

Instance routes require an unscoped token owned by an instance admin. Workspace
tokens cannot configure the instance. Configuration response bodies omit saved
Atlassian secrets; request bodies for both connectors are redacted by API logs.
The MCP server additionally requires `PLANE_ENABLE_INSTANCE_TOOLS=1` and confirmation
for changes. Setup and OIDC configuration remain in God Mode's UI.

## Deployment and smoke checks

1. Pull this Plane backend and apply the normal pending migrations before serving
   clients. Restart API and worker. Keep Live and storage available.
2. Install/update the 22lab MCP checkout and run `uv sync`. Set `PLANE_BASE_URL`
   to an origin serving `/api/v1`, a token and workspace slug.
3. Restart the MCP process and reconnect clients. Check the 36-tool catalog.
4. Read Project Files and import history using a member token. Verify an inactive
   member/workspace-bound foreign token is denied and a guest cannot mutate files.
5. In a test project, upload a file, verify it, add a revision and explicitly activate
   it. Confirm the configured upload hostname resolves from the MCP host.
6. Configure connectors in God Mode, start a small import, read counts/failures,
   then exercise selected retry and incremental sync. Preserve failure reasons.

Regression command:

```bash
docker compose -f docker-compose-test.yml run --rm api-tests pytest \
  plane/tests/contract/api/test_extension_tokens.py \
  plane/tests/contract/app/test_confluence_import.py \
  plane/tests/contract/app/test_jira_import.py \
  plane/tests/contract/app/test_file_upload.py \
  plane/tests/contract/app/test_workspace_bookmarks.py \
  plane/tests/contract/app/test_page_folders.py
```

These tests use isolated data and test storage; they do not mutate a real workspace.
