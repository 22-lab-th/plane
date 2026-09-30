# Import a Confluence space into Plane

## Direct Atlassian API import

In **God Mode → Confluence**, enter the Confluence Cloud site URL
(`https://your-site.atlassian.net`), Atlassian account email and API token, enable
imports, save and test the connection. Leave the token blank on subsequent saves to
retain it. Disable imports before removing the saved token. Tokens are encrypted
with the instance's existing secret key and are omitted from configuration responses
and request logs. This configuration is database-backed, independent of `SKIP_ENV_VAR`.

For scoped API tokens, enter the Atlassian Cloud ID UUID; requests then use
`https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/api/v2`. Classic tokens use
the configured site directly. The account must be able to read the selected space,
its pages and attachments. Grant scoped tokens `read:space:confluence`,
`read:page:confluence` and `read:attachment:confluence`.

In a project's **Pages → Import Confluence → Atlassian API**, select a space and
choose **Import / sync space**. The initial public/private tab and destination folder
determine the import destination. A space already connected to this project keeps
its established destination and owner. Attachments use Project Files access and are
visible to project members, including attachments referenced by private pages.

The worker inventories pages and attachments using paginated Confluence REST API v2
requests, uploads files through the existing quota and MIME verification lifecycle,
and replaces the page's HTML, JSON and Yjs content through Plane Live. Images and
supported videos retain their position relative to document text. Confluence parent
pages have a mapped folder containing their own page and descendants.

Each run persists its phase, total discovered items, completed/failed/skipped/pending
counts and a breakdown of Pages, images, videos, audio and individual document MIME
types. Totals remain explicitly incomplete while discovery is running or if an
attachment listing fails. The item table includes failure codes and readable reasons;
it is paginated and can be filtered to failures. Close and reopen the modal to resume
monitoring the job. The history menu exposes the last 20 project runs.

- **Sync new / updated items:** import new, version-changed, missing destinations
  and previously failed items. Unchanged items are skipped.
- **Retry all failed items:** retry failed and never-successfully-imported items.
- **Retry selected:** retry up to 500 selected item IDs from the connected source.
- **Re-import all / overwrite:** replace all current imported pages and files,
  including local edits, retaining their Plane IDs. Previous file versions and page
  snapshots use the existing retention policies.

Attachment retries also rebuild their owner pages to repair embedded references.
If embedded media is missing, the page fails with `media_dependency_failed`; its
previous content is retained. Retry the failed attachment and its dependent page, or
use Retry all failed. A source page that changes during import fails with
`source_changed` so the next sync uses a consistent version. Removed source items are
not deleted from Plane. Locked, archived, moved or trashed destinations must be
restored/unlocked before retrying; the importer does not silently undo these actions.

There is at most one active run per project/site/space. Workers recheck the initiating
user's project and workspace permissions. Private imports are visible and writable
only by their original owner. Runs with no heartbeat for 15 minutes are marked failed
when viewed or restarted; completed mappings remain available for recovery. Late
worker responses are checked before applying destination changes.

Downloads are bounded by `PROJECT_FILE_MAX_BYTES`, have a three-minute streaming
deadline, validate the remote file size, use DNS-pinned public HTTPS connections and
do not forward credentials to attachment CDNs. HTTP 429/502/503/504 responses have
three bounded attempts. External media that is not a readable Confluence attachment
is reported as unavailable. Exact macro behavior and Confluence CSS are not migrated;
this connector supports Confluence Cloud.

Deployment requires **api, worker, web, admin and live** rebuilt together, migrations
`db.0131_confluence_import_jobs` and `license.0010_confluence_configuration` applied,
the existing broker/object storage running, and `LIVE_SERVER_SECRET_KEY` shared between
the worker and Live. `PLANE_YJS_REPLACE_URL` defaults to `http://live:3001`, the internal
document conversion endpoint. Credentials are entered in God Mode after deployment.

API references: [basic auth](https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/),
[pages](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/),
[attachments](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-attachment/).

## HTML ZIP import

In the destination project's **Pages**, choose **Import Confluence**, select a
Confluence **HTML space export ZIP with attachments**, then choose **Import space**.
Keep the browser tab open until the report appears. Select a public or private Pages
tab and an optional destination folder before importing; the imported folders and
pages inherit that selection.

Confluence export instructions:
https://support.atlassian.com/confluence-cloud/docs/export-content-to-word-pdf-html-and-xml/

## Imported content

- Page titles, paragraphs, headings, lists, tables and links between exported pages.
- Page ancestry from the export's breadcrumbs. Plane CE permits only folders as
  parents, so each source ancestor gets a folder containing its page and descendants.
- Local images and MP4, WebM and Ogg videos at their source positions relative to the
  page's text. Browser codec support still determines whether a video plays.
- Attachments, including unreferenced files under `attachments/`, stored through the
  existing Project Files reserve/upload/verify flow with quota and MIME checks.
- Shared media uploaded once per import and linked to each referencing page.

Documents store `project-file:<id>` references. The editor obtains a fresh, authorized
preview URL when opening the page; expiring storage URLs are never saved as content.
Other attachment types open the project's Files drawer through a stable link. SVG
and script-capable types retain the existing forced-download behavior.

## Limits and recovery

The ZIP must be at most 50 MB, contain at most 500 entries, and expand to at most
100 MB. Individual HTML pages must be at most 5 MB. These are the existing bounded
ZIP extraction limits. File uploads additionally obey the server's configured file
size, MIME allowlist and storage quota.

XML exports and archives without recognizable Confluence page content are rejected.
Remote media is not fetched; include it in the export. Missing or failed embedded
media gets an in-place placeholder and a warning. Active markup and export navigation
are omitted. Confluence macros retain their exported HTML representation where it
fits Plane's editor; exact CSS layout and interactive Confluence macros are not migrated.

A failed content save triggers best-effort archival of pages/folders created by the
current import. Uploaded Files remain available for recovery. Importing the same ZIP
again creates a new copy of its pages and files; it does not synchronize an existing
import.

## Verification

`pnpm --filter web check:test` includes ZIP fixtures, hierarchy and internal links,
media positions, linked images, legacy video objects, shared uploads, missing files,
quota errors, rollback and an HTML/Yjs round trip through the built editor.

The API unit tests cover sanitizer preservation of video references, removal of
script sources and video content signatures. The Docker contract tests additionally
cover persistence on both page-write routes and inline video delivery:

```sh
docker compose -f docker-compose-test.yml run --rm api-tests pytest \
  plane/tests/contract/app/test_page_embed_html_mirror.py \
  plane/tests/contract/app/test_file_download.py
```

The direct API unit and contract tests additionally exercise paginated discovery,
credential-safe redirects, type counts, immutable IDs, version retention, retry and
sync selection, queue failures, stale recovery, permissions and encrypted credentials:

```sh
docker compose -f docker-compose-test.yml run --rm api-tests pytest \
  plane/tests/unit/utils/test_confluence.py \
  plane/tests/contract/app/test_confluence_import.py
```

Contract tests use real database/upload/quota/version code with fake Atlassian and
object storage providers. Set `CONFLUENCE_TEST_LIVE_URL` to an isolated document
replacement server using `confluence-test-secret` to additionally exercise the actual
Live HTML/JSON/Yjs conversion endpoint. UI tests verify progress, failure reasons and
selected retries.
