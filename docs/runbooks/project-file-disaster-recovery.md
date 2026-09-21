# Runbook: project-file disaster recovery

Scope: the project-file-storage feature's data — the `file_objects`,
`file_versions`, `file_links` and `file_access_logs` rows in the 22lab
PostgreSQL database, and the objects those rows name in the bucket (R2 in
production, MinIO in the local stack). Requirement R-NFR-7; acceptance AC-32.
A recorded restore drill is in §4.

**The database backup and the object store are one recovery unit.** Neither half
is a recovery on its own: with the rows restored and the objects missing, the
application answers normally and every download fails at the fetch (§4, step 8 —
observed, not assumed); with the objects restored and the rows missing, the
objects cannot be reached at all, because no application job deletes or lists
keys the database does not name (AD-12, AD-13). Restore both, from the same
point in time, or you have restored nothing.

Owner: the **22lab owner** — the accountable role recorded in the decision log
and named in the erasure runbook. The database backup's schedule, retention,
medium and off-site copy are **deployment configuration, not application code**,
and none of them is recorded in this repository (§6). This runbook states what
the restore must consist of and how to prove it worked; it does not invent a
backup policy.

## 1. What the recovery unit contains

| Component                                                                                             | What it holds                                                                                                                                                                                                                                                                                                  | Where it comes from                                                                                                                                                                   | Covered by the drill |
| ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| PostgreSQL database (`plane`; `POSTGRES_DB`)                                                          | Every row the feature depends on: workspaces, projects, users, memberships, `file_objects.object_key`, every `file_versions` row (key, `size_bytes`, `etag`, `status`, `is_active`, `object_deleted_at`), `file_links`, `file_access_logs`, and the quota counters (`storage_quotas`, `project_storage_usage`) | The deployment's existing PostgreSQL backup process (R-NFR-7 inherits it; this repository defines none)                                                                               | yes, §4              |
| The bucket (`AWS_S3_BUCKET_NAME`: `plane-files-prod`, `plane-files-dev`; `uploads` in the test stack) | The object bytes, addressed by `file_versions.object_key` / `file_objects.object_key`                                                                                                                                                                                                                          | Whatever copy 22lab holds. R2 exposes no object-versioning API (SPEC constraint 3), so **`file_versions` is the only recovery map** and a key must never be re-keyed during a restore | yes, §4              |

Not part of the recovery unit, but required to use it: the bucket name and the
R2 API token, `SECRET_KEY`, the `AWS_*` deployment configuration, and the
release revision that matches the dump. Keep them where the restore operator can
reach them without the failed system.

Two properties that decide what a restore can be:

- **Object keys are write-once and unique per file id and version** (DEC-001). A
  restore that changes a key breaks the row that names it, permanently: the old
  object is unreachable and the row points at nothing. Copy objects back to the
  keys the rows carry, and nothing else.
- **An object whose row does not exist is invisible, not merely orphaned.** The
  application never lists the bucket to discover work: the unverified-object
  sweep evaluates its predicate in the database and deletes exact stored keys
  (AD-13), and no lifecycle rule targets a project prefix (AD-12). A stray
  object therefore sits in the bucket with nothing pointing at it and no job
  that will ever collect it — clean it up by hand or not at all.

## 2. The restore procedure

Roles: the **operator** executes; the **22lab owner** declares the incident and
accepts the recovery point. Steps 1–2 are the restore; steps 3–4 are the proof
that the recovery unit is whole — and step 4 is the one that is usually skipped,
because after step 3 the API looks healthy again.

**1. Freeze and record.** Stop the API and the workers (or put them in
maintenance mode) so no writes, uploads, purges or sweeps run against either
half while it is being replaced; note the time writes stopped. Record the
recovery point you are targeting — the timestamp of the newest database backup
that is known-good, and the object copy made alongside it. If only one of the
two exists for that timestamp, stop and say so: the other half's RPO is
undefined.

**2. Restore the database, then the objects.**

```bash
# database — custom-format dump, then restore into an empty database
pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/plane.dump
psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\" WITH (FORCE)"
psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\""
pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges /tmp/plane.dump
python manage.py migrate --noinput   # when the dump predates the release that will run

# objects — copy the mirrored tree back under the keys it came from
mc mirror --overwrite /path/to/object-backup/uploads local/uploads   # MinIO / S3-compatible
# against R2: the same relative paths through an R2 alias, rclone, or the provider's
# tool. Never rename a key.
```

These are the flags the drill ran; the transcript is the exact record, and the
drill issued each command inside the container that owns the data
(`podman exec … pg_dump`, `podman cp` for the dump file, `mc mirror` inside the
MinIO container). Substitute the deployment's connection settings for the
container form — nothing else changes.

The exact commands as executed in the recorded drill — with their outputs,
exit codes and timings — are in §4; the transcript is
`artifacts/evidence/T-121/transcript.txt`.

**3. Sweep every live version row against the bucket.** One command; runs inside
the API container or a deployment shell with the application's environment:

```bash
python manage.py shell -c "from plane.db.models import FileVersion; from plane.settings.storage import S3Storage; storage=S3Storage(); rows=list(FileVersion.objects.filter(status__in=['active','superseded'], object_deleted_at__isnull=True)); bad=[(str(v.id), v.object_key, v.size_bytes) for v in rows if (storage.get_object_metadata(v.object_key) or {}).get('ContentLength') != v.size_bytes]; print('live_versions=%d size_mismatches=%d' % (len(rows), len(bad))); print('mismatches=%s' % bad)"
```

`live_versions=N size_mismatches=0` is the pass. Anything else is a row whose
object is missing or truncated; a missing object means the object half is
incomplete, and the row cannot be repaired by deleting it — re-copy the key,
then re-run.

**4. Prove a fetch, not just a HEAD.** The download endpoint signs a URL without
consulting the bucket, so it returns `200` even when the object is gone (that is
exactly what §4 step 8 records). Fetch a sample of files through the API and
compare the bytes. The reference implementation is the drill's
`artifacts/evidence/T-121/drill_verify.py`: it calls
`GET /api/workspaces/{slug}/projects/{project_id}/files/{file_id}/download/`,
fetches the signed URL the endpoint returns with a plain HTTP client, and
compares the sha256 of the response body with the pre-disaster sha256
(`--expect full` against a restored system, `--expect degraded` to reproduce the
half-restored state and prove the endpoint's `200` lies). Use it, or the
equivalent, against a sample that includes one file of each category.

**5. Rebuild the derived numbers, then reopen.** `reconcile_storage_batch()` in
`plane/bgtasks/file_quota_task.py` recomputes project and workspace usage from
the restored rows (the daily job calls it as `reconcile_storage_usage`). Its
bucket cross-check calls `S3Storage.get_bucket_usage_bytes()` — the provider's
usage summary (RSCH-001 S15); the method's own docstring names MinIO in this
environment as a provider that cannot answer, and a `None` is reported as
`bucket_usage_available: false` rather than as a clean bucket, so the
row-vs-bucket comparison is **skipped, not passed**. Then reopen writes and
record: the recovery point, the incident window, what the sweep and the fetch
sample reported, and any key the restore could not verify.

## 3. What this runbook cannot promise

Each of these is a thing a reader might assume a "disaster recovery" document
covers. It does not, and none of them is claimed elsewhere in this repository:

- **Provider snapshot semantics.** R2 exposes no object-versioning API (the
  `Put/GetBucketVersioning` operations are listed unimplemented — SPEC
  constraint 3 records the "no versioning at all" reading as an `[INFERENCE]`
  with a deferred probe). Nothing in this design relies on the provider holding
  a previous copy of an object; the object half of the recovery unit is the copy
  22lab takes. An overwrite or delete on a key is unrecoverable through the
  storage layer.
- **Cross-region copies.** Not promised: ARCH-001 §6 records that cross-region
  replication is not documented for R2 in the retrieved sources. A single-bucket
  deployment has exactly the durability its provider offers and whatever copy
  the backup job makes.
- **Retention, schedule and medium of the database backup.** Owner/deployment
  configuration. The erasure runbook records the same gap for the same reason
  (its §3.2), and R-LEG-2 makes the backup copies part of the erasure scope.
  Until the owner fixes these numbers, RPO cannot be stated — R-NFR-7 says only
  that the deployment's RPO/RTO must not be worsened.
- **The drill proves the procedure, not production scale.** The recorded run
  restores one 1 MiB object on a local Postgres 15.7 and MinIO; its seconds are
  not an RTO. It also does not exercise: R2's own copy/restore tools, R2 Data
  Access Logs, the production token and bucket, a dump large enough to need
  parallel restore, or a restore into a different host.
- **The legacy `FileAsset` objects.** They live in the same bucket under their
  own keys with their own rows, so the same procedure moves them; nothing here
  verifies the legacy asset surface. The regression guarantee is T-117's.
- **Backup of anything else in the database** (issues, pages, SSO identities,
  etc.) is covered by the deployment's PostgreSQL backup — the same dump — but
  this runbook only verifies the file surfaces.

## 4. The recorded restore drill

Run date **2026-09-21**, recorded at head `74ab9a57ac`, on this machine against
an isolated podman stack (project `drdrill`; `podman 6.0.0`,
`podman-compose 1.6.0`; `postgres:15.7-alpine` + the MinIO image the test stack
pins — the S3-compatible stack the contract suite is proven against). The API
code under test is the working tree mounted into `localhost/plane_api-tests:latest`.
The drill's inputs are the local containers, that image and the seed script;
the documentation commit that adds this runbook changes none of them. Script:
`artifacts/evidence/T-121/drill.sh`; transcript with every command, exit code
and timestamp: `artifacts/evidence/T-121/transcript.txt`; the two helper scripts
it drives (`drill_seed.py`, `drill_verify.py`), the manifest, the dump, the
object copy and the pre/post inventories are in the same directory.

What the drill did, in order:

| Step | What happened                                                                                                                                                                                                                                                                                                        | Observed result                                                                                                                                                                                                                                                                                                                                                   |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1–2  | Started the stack, migrated the drill database with the product's own migrations                                                                                                                                                                                                                                     | both services healthy; migrations applied, exit 0                                                                                                                                                                                                                                                                                                                 |
| 3    | Seeded one file through the product's code path: workspace → project → `build_object_key` → `S3Storage.upload_file` → `file_objects` + `file_versions` rows                                                                                                                                                          | 1 MiB, sha256 `09358cb6…d107fcab`, ETag `b4790067…920354c` recorded in `manifest.json`                                                                                                                                                                                                                                                                            |
| 4    | Baseline verification (see below)                                                                                                                                                                                                                                                                                    | 5/5 checks PASS, exit 0                                                                                                                                                                                                                                                                                                                                           |
| 5    | Pre-disaster inventory: row counts, the version row (id, key, size, ETag), the full object listing as JSON                                                                                                                                                                                                           | recorded to `pre-db-rows.txt`, `pre-objects.jsonl`                                                                                                                                                                                                                                                                                                                |
| 6    | **Backup:** `pg_dump -Fc` → `backup/plane.dump` (724 588 bytes, sha256 `f538f706…05ca69d9` in the transcript); bucket mirrored → `backup/objects/uploads/<key>` (object sha256 `09358cb6…d107fcab`, identical to the seeded bytes); the in-container mirror was then deleted so the only copy is outside both stores | backup_elapsed **1 s**                                                                                                                                                                                                                                                                                                                                            |
| 7    | **Destroy both halves:** every object removed from the bucket; the database dropped (`DROP DATABASE plane WITH (FORCE)`) and recreated empty                                                                                                                                                                         | object listing empty; `public_tables=0`                                                                                                                                                                                                                                                                                                                           |
| 8    | **Restore the database only**, then verify                                                                                                                                                                                                                                                                           | rows present (1 file, 1 version) and the endpoint answers `200` with a signed URL, but the fetch is **HTTP 404 `NoSuchKey`** and the HEAD returns nothing → verification exits 0 only because "degraded" was the expected observation. **This is the drill's central result: a database-only restore looks healthy and is not.** restore_database_elapsed **3 s** |
| 9    | **Restore the objects**, re-run the verification with "full" expected                                                                                                                                                                                                                                                | 5/5 checks PASS: rows consistent, HEAD size matches, endpoint `200`, fetched sha256 equals the pre-disaster sha256, audit row present → total restore **5 s**                                                                                                                                                                                                     |
| 9b   | The operator command of §2 step 3, verbatim                                                                                                                                                                                                                                                                          | `live_versions=1 size_mismatches=0`                                                                                                                                                                                                                                                                                                                               |
| 10   | Post-restore inventory compared with the pre-disaster one                                                                                                                                                                                                                                                            | object identity (key, size, ETag) identical, database rows identical, table count identical — three diffs, all empty. The raw object listing differs in exactly one field: `lastModified` (the restore rewrote the object)                                                                                                                                        |
| 11   | Summary                                                                                                                                                                                                                                                                                                              | every step's exit code 0 → `DRILL RESULT: PASS`; total wall clock 83 s                                                                                                                                                                                                                                                                                            |

The two verification phases report five checks each:

1. the `file_objects` / `file_versions` rows exist and agree with the manifest;
2. the object is in the bucket, read back through `S3Storage.get_object_metadata`
   (HEAD) with the recorded size;
3. `GET …/files/{file_id}/download/` answers `200` and carries a signed URL;
4. fetching that signed URL returns `200` and a body whose sha256 equals the
   pre-disaster sha256;
5. the `downloaded` row the endpoint wrote to `file_access_logs` is present
   (the audit tail survives the disaster and the restore).

What the drill demonstrated: the backup and restore procedure in §2 is
executable and repeatable; the two halves really are one recovery unit (a
database-only restore fails only at the fetch); a completed restore is
byte-identical at the object level and row-identical at the database level; the
download path of the restored system serves the original bytes; the audit trail
comes back with the database.

What the drill did not demonstrate, and must not be read into it: anything about
R2 as a provider (its own copy tools, snapshot semantics, tokens, Data Access
Logs, jurisdictional behaviour); the production bucket or database; the backup
schedule, retention or medium; a restore at production size; a restore on a
different host; and the reconciliation job of §2 step 5, whose bucket cross-check
the local MinIO cannot answer. The drill also did not exercise an _upload_
through the restored system — only the read path.

## 5. After a real recovery

Record as the incident's recovery evidence: the recovery point (dump timestamp
and the matching object copy), the incident window, the outputs of §2 steps 3–5,
every key that could not be verified and what was done about it, the restored
revision (`manage.py migrate` output), and the reopen time. Then re-run the
drill's verification against the recovered production system — the drill is the
template, and a production run is the only thing that exercises the provider
half.

## 6. Decisions the owner still owes

| Item                                                                          | Why the runbook cannot state it                                                                                                          | Status                                                   |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| Database backup frequency (→ RPO), retention window, medium and off-site copy | Deployment configuration; nothing in this tree records it. R-NFR-7 only requires that the deployment's existing RPO/RTO is not worsened  | Owner action — same gap the erasure runbook §3.2 records |
| That the object half is captured by the same job, at the same timestamp       | This runbook's "one recovery unit" claim is only as good as the backup job; if the copy is taken separately, the two halves can disagree | Owner action                                             |
| Who executes a restore (role and named person)                                | The runbook names the role, not a person                                                                                                 | Owner action                                             |
| R2 account, bucket and token                                                  | Blocks real-R2 evidence entirely; the bucket cannot be provisioned before the owner provisions it (ARCH-001 §10)                         | Owner action, tracked as the deferred real-R2 threshold  |
| Whether R2 Data Access Logs are enabled                                       | Needed by the breach runbook's evidence checklist; delivery is "best effort" even when enabled                                           | Owner action                                             |
| A restore drill against production R2 and a production-sized database         | The local drill cannot exercise the provider half                                                                                        | Owner action, before production acceptance               |
