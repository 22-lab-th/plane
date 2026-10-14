# Plane production upgrade, 14 October 2026

Plane at https://plane.22lab.dev reopened at **16:10:53 Asia/Bangkok** on 14 October 2026. Coolify reports `running:healthy`; all 12 persistent services are healthy and the migrator exited 0. The frozen cutover phase took approximately 13.5 minutes.

## Release

- Deployed source: `bc40bb70658c55fabc1b4125d173c7c4c074759f`, based on the latest `22-lab-th/plane` main `72c21ef4f0` and a Page history fix found during integration testing.
- All six application images (backend, web, admin, space, Live, proxy) were built from that revision, verified by archive checksum, image revision and immutable digest, then loaded before cutover.
- [Release build](https://github.com/22-lab-th/plane/actions/runs/38537227314): all six jobs passed.
- API, worker, beat and migrator use the same backend digest. PostgreSQL, MinIO, Redis and RabbitMQ images, data volumes, storage identity and credentials were preserved.
- Both Coolify's persisted raw source and the effective host compose were updated. Their changes are limited to application images; effective environment and volume mappings were checked before startup.

## Data preservation evidence

- A new frozen database/object/configuration backup was encrypted and copied off-host, then decrypted and actually restored into an isolated PostgreSQL container with no network.
- All **124 original tables** matched the frozen backup's original columns, row counts and row-content digests after restore.
- Immediately after migration, **120 original tables were unchanged**. Four metadata/configuration tables only appended rows; every original row was verified retained.
- After production MCP write tests and fixture cleanup, these business tables matched the frozen originals exactly:

| Data                    | Original and final count |
| ----------------------- | -----------------------: |
| Users                   |                        6 |
| Workspaces              |                        3 |
| Projects                |                       21 |
| Work items              |                      233 |
| Pages                   |                      113 |
| Page versions           |                       27 |
| Legacy file assets      |                       64 |
| Project files           |                      112 |
| File versions           |                      112 |
| File links              |                      111 |
| Workspace bookmarks     |                       32 |
| Bookmark groups         |                        7 |
| Original instance admin |                        1 |

All **176 original storage objects**, totaling **56,787,089 bytes**, matched SHA-256, object metadata and delivery metadata. After cleanup the bucket again contained exactly those original keys. Seven synthetic test objects, one synthetic workspace and its synthetic user were removed. Original user data was not deleted or rewritten.

Frozen encrypted backup SHA-256:

```text
da06d29cf05e4cb7f67ffa483fa01f367fd2c3c40a2133fb05e299a2a6a2a6f03a
```

Private backup, recovery key, release manifest and detailed receipts:

```text
/Users/teeppiphat/.local/share/plane-deployment/20261014T082510Z/
```

The directory is private. Credentials, database dumps, storage keys and real content are not committed to Git.

The existing MinIO image is also preserved off-host in `minio-runtime-image.tar.enc`, using the same recovery key. Encrypted readback, the original image manifest digest and every archived blob hash were verified. Its encrypted archive SHA-256 is `aa87fcc4c193b42f72071095be51c827cc3e7028926fff4e25a3d5ed72fc4985`. This protects recovery because the former public image could no longer be pulled during the new CI rehearsal. Production continues using its original image.

## Functional verification

- Backend: **149 tests passed**, covering extension-token authorization, Jira/Confluence import handling, upload verification, bookmarks, folders and Page history. The tests used isolated storage and a test database.
- Frontend: **46 tests passed** across eight files, including import dialogs, Files sharing and Markdown conversion.
- A fresh stdio MCP process exercised **66 distinct actions** against both the final isolated image stack and production under operator-only maintenance access.
- Verified Page content and hierarchy, fractional ordering, lock/access/favorites, version creation/retrieval, Page assets, archive/restore/delete and cross-project moves.
- Verified file folders, reserve/PUT/complete/abort, byte-for-byte downloads of two revisions, version activation, entity links, copy/move across projects, activity, trash/restore/purge and stable Files links.
- Verified bookmark/group CRUD, Markdown file/ZIP import with images and Confluence HTML ZIP import with attachments.
- Core/extension read checks passed again after public traffic reopened.
- [Cross-repository CI](https://github.com/22-lab-th/plane-mcp-server/actions/runs/38545920316) passed the full MCP regressions and all 66 actions on a fresh disposable database, building the selected backend, Live and pinned official MinIO source. The CI deployment uses `API_KEY_RATE_LIMIT=600/minute` because an import makes several API requests inside one tool call. This setting is confined to CI; production limits were preserved.

The integration run found that API-token Page content updates did not enqueue version history. The deployed fix dispatches history only after the database transaction commits; project/workspace content writes and metadata-only writes have regression coverage.

## Local MCP

The local checkout at `/Users/teeppiphat/Projects/22lab/plane-mcp-server` was upgraded to **0.4.1**. Its full suite passed **1,630 tests**, with 36 skipped; Ruff lint and format checks passed. A fresh stdio connection verified 36 tools, 284 actions, extension readiness, new action schemas, fractional Page ordering and correct error flags. `doctor --require-extensions` passes against production.

The connection now explicitly uses the `22lab` backend profile, logs no tool payloads and permits imports from the existing `life` directory plus the two explicit Plane/MCP repository directories. The existing API credential was preserved.

Existing clients may cache tool schemas until reconnecting. The installed package and a fresh stdio connection have been verified; unrelated running Codex sessions were not terminated.

After reopening, read-only MCP checks also passed for **WILDFIRE**, project `3f29f9c7-6542-4b35-bda6-43a8df7457cd`. It has six work items, zero Pages and zero Project Files. Project retrieval, Page listing/summary, Files listing and storage usage returned successfully. No WILDFIRE data was changed.

## Limits and existing issues

- The user excluded real Jira/Confluence testing. Import logic/recovery was verified with backend fixtures and offline Confluence ZIP imports; no real Atlassian service import was performed. No connector credentials were added.
- The public login page emits React hydration warning #418 and then renders successfully. The identical warning was reproduced using the exact previous production web image digest in the existing isolated rehearsal stack. It is a pre-existing issue; the God Mode login page rendered successfully.
- R2 was not enabled or tested. Production retains its existing MinIO store.
- Traffic is accepting new writes again. Do not restore the pre-upgrade snapshot as an ordinary rollback without first preserving and reconciling writes accepted after reopening.
- No recurring backup or monitoring automation was created.
