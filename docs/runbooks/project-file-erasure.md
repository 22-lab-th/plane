# Runbook: project-file retention and erasure

Scope: project files (`file_objects` / `file_versions` / `file_links`) in the
project-file-storage feature. Requirement R-LEG-2 (PDPA s. 37(3); PDPC Erasure
Notification B.E. 2567 clauses 2–4, 9): a data-subject erasure request for a project
file is fulfilled **within 90 days** with written confirmation of completion.

Owner: the 22lab owner (the accountable role recorded in the decision log). Legal scope
confirmation remains an owner action — this runbook states what the _mechanism_ covers
and makes **no legal claim** that the exclusions below are sufficient.

## 1. What the automated mechanism covers

The purge is the erasure mechanism. It is application-driven; no R2 lifecycle rule is
used for project files (AD-12), so nothing is deleted except keys the database knows.

| Step                                              | Who           | How                                                                                                                                             |
| ------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Locate the file                                | Project ADMIN | `GET /api/workspaces/{slug}/projects/{project_id}/files/{file_id}/` (add `?trashed=true` variants, or `GET .../files/?q=` for the live surface) |
| 2. Move it to the trash (skip if already trashed) | Project ADMIN | `DELETE /api/workspaces/{slug}/projects/{project_id}/files/{file_id}/`                                                                          |
| 3. Purge                                          | Project ADMIN | `DELETE /api/workspaces/{slug}/projects/{project_id}/files/{file_id}/purge/?confirm=true` (irreversible; `confirm=true` is required)            |
| 4. Record the completion                          | Project ADMIN | Read back the `purged` audit row: `GET /api/workspaces/{slug}/projects/{project_id}/files/activity/?file_id={file_id}&action=purged`            |

What step 3 removes, in this order (ARCH-001 §4.4): **every version object** at its exact
key, then the `purged` audit row, then the file row — whose versions and links go with it
by cascade. Links removed include the ones the trash had already marked inactive. The
project's usage counters drop by the purged bytes in the same transaction.

What step 3 does **not** remove: the audit skeleton. `file_access_logs.file_id` is a
plain id, not a foreign key, so the rows describing the file survive its deletion
(R-NFR-9, AC-27). Step 4 is the evidence: the row carries the actor id, the timestamp,
the action (`purged`), the file id and name snapshot, and `metadata.object_keys` — the
exact keys that were deleted.

What happens if storage refuses: the purge deletes versions one at a time, and a
storage failure aborts the loop on the failing call. Versions the loop had already
deleted before the failure are gone (their version rows carry `object_deleted_at`
and the failed version marks `purge_failed`); the failing version and the ones
after it are still stored. No `purged` audit row is written, so the **completion
record** the operator would read in step 4 is missing — that is what the failure
aborts. The file row's status becomes `purge_failed`, the endpoint answers
`502 storage_unavailable`, and the daily `purge_expired_files` task retries every
`purge_failed` row regardless of age. The state is "some objects may already be
gone, completion record absent, repair pending" — not "objects stay" — so a
`502` is **not** a completed erasure. Read step 4 back before confirming
completion; the repair path (restore, then re-activate a version whose object is
intact, or the next retry) is what restores the invariant.

The audit residue itself is masked, not deleted: `mask_audit_pii` clears `ip_address`,
`user_agent`, `actor_display` and `file_name_snapshot` after `AUDIT_PII_RETENTION_DAYS`
(default 90), keeping actor id, action, target id and timestamps for the project's
lifetime (R-NFR-13). That is the "residue in the audit log" R-LEG-2 names.

## 2. Retention configuration

`Project.retention_days` is the per-project window, set through the project API
(`PATCH /api/workspaces/{slug}/projects/{project_id}/` with `{"retention_days": <days>}`,
or `null` to fall back). It is the value the PDPA s. 23(3) notice and the s. 39 record
set cite, so record it for the project's notice once set.

- `null` → `PROJECT_FILE_TRASH_DAYS` (owner-set default **30** days).
- The purge selects a trashed file once `deleted_at + window` has passed, and the restore
  endpoint refuses the same file (`409 retention_expired`): a file is restorable exactly
  while it is not yet purgeable, and the purge wins at the boundary (R-LIFE-1) —
  except a `purge_failed` row whose window has _not_ yet elapsed: the purge has
  selected it, but the restore endpoint deliberately returns `200` so a partial purge
  can be repaired, which the activation path enforces by refusing a version whose
  bytes are gone.
- The API refuses a value below 1 day. A stored `0` would be read as "not set" by the
  purge, which would silently grant a 30-day window to a project that asked for none.
- The setting is read back on the project detail, create and update responses
  (`GET …/projects/{id}/`, `POST …/projects/`, `PATCH …/projects/{id}/`) and on the
  workspace detail list (`GET …/projects/details/`). The plain workspace project
  list (`GET …/projects/`, the `.values(...)` row at `views/project/base.py:175-223`)
  currently omits it. Read the setting from the project detail when a quick-view is
  needed.

## 3. Holdings outside the automated mechanism

A purge removes the objects and links of the file the request names. These three holdings
are **not** reached by it, and each needs its own step, executed by the role named.

### 3.1 Legacy `FileAsset` objects

**Not covered.** Profile/logo/cover and description assets uploaded through the legacy
asset paths are `file_assets` rows with their own objects, and their endpoints keep
working unchanged (R-NFR-10, R-DEL-5). An erasure request that covers such a file is a
separate, manual step.

- Who: **22lab platform owner**, on a written request from the workspace ADMIN.
- Procedure: read the exact key from `file_assets.asset`, delete that key from the bucket
  with the operator's own credentials, then remove the row. The legacy delete endpoints
  (`DELETE /api/assets/v2/workspaces/{slug}/{asset_id}/` and
  `DELETE /api/assets/v2/user-assets/{asset_id}/`) only set `is_deleted`/`deleted_at` —
  they **do not** remove the stored object, so they are not an erasure.
- Record: the key, the bucket, the timestamp and the operator in the completion note.

### 3.2 22lab database backups

**Not covered.** A purge deletes rows and objects in the live system; it does not rewrite
backups that already contain the file's metadata (`file_versions.object_key`,
`file_access_logs`). Those copies expire only when the backup retention policy expires
them.

- Who: **22lab platform owner** (the backup schedule and retention are deployment
  configuration, not application code).
- Procedure: confirm the request against the backup retention window in force, and either
  wait for expiry or restore-and-repurge the affected metadata rows if the policy
  requires the data gone sooner. Record which of the two was done.
- Record: the backup set(s) in scope, the retention window, the action taken, the date.

### 3.3 Provider-side copies (Cloudflare R2 and any processor replica)

**Not covered by any application code.** Object deletion on the API path is strongly
consistent, so the object is gone from the bucket when step 3 returns (`204`), but copies
held by a processor are outside this system's reach.

- Who: **22lab owner**, through the Cloudflare DPA delete-or-return clause.
- Procedure: raise the request on the provider channel and file the written response.
  Cloudflare's DPA promises notice and action "without undue delay" rather than within a
  fixed clock, so the processor leg is tracked in parallel with the 90-day bound rather
  than assumed to finish inside it.
- Record: the request reference, the date sent, the provider's written response.

### 3.4 What is not promised

No claim is made that 3.1–3.3 are sufficient in law. Whether these exclusions satisfy
the erasure obligation for this data is an open legal question outside this runbook —
the requirement is R-LEG-2, the erasure-scope sufficiency is R-LEG-2's residual, and
the engineering side delivers the mechanism regardless. The owner decisions recorded
against R-LEG-4 (no external counsel engaged, 2026-09-20; DPA assessment not performed;
residual risk accepted with revisit triggers) and R-LEG-5 (default R2 placement, no
jurisdiction) are accepted owner decisions, not open blockers — and the erasure-scope
sufficiency is not one of the items the runbook can close.

## 4. Completion record

The written confirmation to the requester states: the file id and display name, the
project, the date the request was received, the date the purge completed, the actor id of
the operator who ran it, the version count and the object keys from step 4's `purged` row,
and for each item in §3 the action taken, the operator and the date — including "not
applicable" where the file was never a legacy asset. The `purged` audit row and the
provider response are the evidence attached to it.
