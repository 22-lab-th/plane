# Import a Jira Cloud project into Plane

## Configuration

Open **God Mode → Jira**. Enter the Jira Cloud site URL
(`https://your-site.atlassian.net`), Atlassian account email and API token, enable
imports, save, then test the saved connection. Leave the token field blank on later
saves to retain it. Disable imports before removing the token. Configuration is
database-backed; the token uses existing instance encryption and is omitted from
configuration responses and request logs. Jira and Confluence settings are independent.

Classic API tokens connect to the site directly. Scoped tokens require the Atlassian
Cloud ID UUID and connect through `https://api.atlassian.com/ex/jira/{cloud_id}`.
The account needs visibility of the project, issues, comments, attachments, boards
and sprints being imported. Scoped tokens also need the read scopes listed for each
REST operation, including `read:board-scope:jira-software`, `read:project:jira` and
`read:sprint:jira-software` for sprint discovery. The connection test checks project
visibility; it does not prove that every issue, attachment or sprint is accessible.

## Import and mappings

In an existing Plane project, open **Project settings → Jira Import**, select a Jira
project and choose **Import / sync project**. Importing requires active workspace
and project membership with member/admin access. The job runs in the background;
you can close the page and return to view its persisted progress.

The importer creates or updates:

- Work items: title, description, status, priority, due date, labels and matched assignee.
  Status categories map to Plane unstarted, started and completed groups. Original
  Jira keys, source links and reporter attribution remain available.
- Parent/subtasks and issue relationships within the selected project. Blocked,
  duplicate and other relationships map to their Plane equivalents. Cross-project
  parents fail visibly rather than creating an incomplete hierarchy.
- Comments, including original author attribution and creation time. Jira accounts
  match by visible email to active Plane project members. For hidden emails, use the
  account ID in the result warning to add an explicit **User mapping**, then sync.
  Mappings save with the next import. Unmatched assignees remain unassigned and
  unmatched comments retain the author's name without impersonating the importer.
- Attachments in **Project Files**, linked to their work items. Resolved embedded
  images and supported videos keep their place in the description through durable
  `project-file:<id>` references. Existing file quota, MIME and size checks apply.
- Sprints as **Cycles**, with names, goals and start/end dates. Cycles are enabled
  automatically for the destination project. Plane assigns each work item to one
  cycle: an active sprint, otherwise a future sprint, otherwise the latest closed
  sprint. All visible discovered sprints remain as cycles, including empty ones.

Imported content uses destination project permissions. Jira issue/comment visibility
restrictions are not recreated in Plane. No accounts or invitations are created.
Custom fields, workflows, worklogs and full historical sprint membership are not migrated.

## Progress, sync and recovery

Each run shows its phase, discovered total, completed, failed, skipped and remaining
items, with a breakdown of issue types, sprints, comments, images, videos, audio and
document MIME types. Totals are marked incomplete during discovery or after a
listing failure. Results include failure codes, reasons and unmapped-user warnings,
with pagination and a failures-only filter. History shows the latest 20 project runs.

- **Sync new / updated items** processes new/changed/failed items and skips unchanged
  ones. Each sync rediscovers the source to detect comment and sprint changes.
- **Retry all failed items** retries failed and never-successfully-imported items.
- **Retry selected** processes up to 500 selected source item IDs.
- **Re-import all / overwrite** replaces all current imported content, including
  local edits, while retaining the same work item/comment/file/cycle IDs. Previous
  work item and file versions use Plane's existing retention policies.

Attachment retries rebuild their work item to repair embedded references. Sprint
changes update the same cycle and its imported member work items. Comment-only
updates leave unchanged work items alone. Failed editor conversion or relationship
updates retain the previous work item content. Discovery is repeated on every retry,
so a fixed scope or source permission can recover incomplete inventory.

Missing/unresolved embedded media fails visibly instead of guessing attachment IDs.
Restore removed, moved or archived Plane destinations before retrying. Removed Jira
items and removed issue links are not automatically deleted from Plane. Existing
source items retain mappings, preventing duplicate destinations on later runs.

Only one run per Plane project/Jira site/Jira project may be active. Workers recheck
the initiating user's permissions. Jobs with no heartbeat for 15 minutes become
failed when viewed/restarted; late writes are fenced. Restore worker/broker access
and retry. HTTP 401/403 errors identify credential/scope problems; quota, MIME,
file-size and editor failures include recovery reasons. HTTP 429/502/503/504 have
bounded retries. Downloads use the shared public HTTPS transport, size/time limits
and credential-safe CDN redirects. No Jira writes are performed.

## Deployment and validation

Rebuild **api, worker, web, admin and live** together and apply migrations
`db.0132_jira_import_jobs` and `license.0011_jira_configuration`. Keep the broker and
object storage available. Worker and Live must share `LIVE_SERVER_SECRET_KEY`;
`PLANE_YJS_REPLACE_URL` defaults to `http://live:3001`. Enter actual credentials in
God Mode after deployment. This connector supports Jira Cloud.

```sh
docker compose -f docker-compose-test.yml run --rm api-tests pytest \
  plane/tests/unit/utils/test_jira.py \
  plane/tests/contract/app/test_jira_import.py \
  plane/tests/unit/utils/test_confluence.py \
  plane/tests/contract/app/test_confluence_import.py
pnpm --filter web check:test
pnpm --filter web check:types
pnpm --filter admin check:types
```

Contract tests exercise real database, quota, upload, version and API code with fake
Jira/object storage providers. Set `JIRA_TEST_LIVE_URL` to an isolated document
replacement server using `jira-test-secret` to exercise actual Live HTML/JSON/Yjs
conversion. No production Jira credentials or data are needed for these tests.

References: [authentication](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/),
[enhanced search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/),
[comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/),
[attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/),
[boards and sprints](https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/).
