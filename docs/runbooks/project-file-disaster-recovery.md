# Runbook: project-file disaster recovery

Scope: the project-file-storage feature's data — the `file_objects`,
`file_versions`, `file_links` and `file_access_logs` rows in the 22lab
PostgreSQL database, and the objects those rows name in the bucket (R2 in
production, MinIO in the local stack). Requirement R-NFR-7; acceptance AC-32.
A recorded restore drill is in §4.

**The database backup and the object store are one recovery unit.** Neither half
is a recovery on its own. With the rows restored and the objects missing, the
application answers normally and every download fails at the fetch (§4, step 8 —
observed, not assumed). With the objects restored and the rows missing, the
objects cannot be reached **through the product** at all, because no
file-serving or file-cleanup path finds work by listing the bucket and nothing
deletes a key the database does not name (AD-12, AD-13) — that half is a design
argument from those decisions, not a drill result, and §4 lists it as such. (A
bucket-credentialed operator can still read those objects directly, which is the
next bullet's point.) Restore both, from the same point in time, or you have
restored nothing.

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
- **An object whose row does not exist is invisible, not merely orphaned.** No
  file-serving or file-cleanup path discovers work by listing the bucket: the
  unverified-object sweep evaluates its predicate in the database and deletes
  exact stored keys (AD-13), and no lifecycle rule targets a project prefix
  (AD-12). The one command in the tree that does list the bucket is
  `db/management/commands/update_bucket.py`, which reads the listing to probe
  `s3:ListBucket` and to build a bucket policy; it removes no stored object — its
  only delete is its own `test_permission_check.txt` probe
  (`update_bucket.py:75`). A stray object therefore sits in the bucket with
  nothing pointing at it and no job that will ever collect it — clean it up by
  hand or not at all.

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

The restore itself starts from a backup you already hold. In the drill the
backup was taken from the healthy stack immediately before the destruction — that
is how a drill gets a known-good recovery point — and the commands below are the
pair as executed; **in an incident, take the dump from an existing pre-incident
backup set, never by dumping the database you suspect is damaged** (step 1's
recovery point is what you restore from):

```bash
# taking the backup (the drill's step 6; how the deployment's backup job should read)
pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/plane.dump
mc mirror --overwrite local/uploads /path/to/object-backup/uploads   # the object half, same run

# restoring from it (the drill's steps 8-9)
psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\" WITH (FORCE)"
psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\""
pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges /tmp/plane.dump
python manage.py migrate --noinput   # when the dump predates the release that will run

mc mirror --overwrite /path/to/object-backup/uploads local/uploads   # back under the keys it came from
# against R2: the same relative paths through an R2 alias, rclone, or the provider's
# tool. Never rename a key.
```

These are the flags the drill ran; the transcript is the exact record, and the
drill issued each command inside the container that owns the data
(`podman exec … pg_dump`, `podman cp` for the dump file, `mc mirror` inside the
MinIO container). Substitute the deployment's connection settings for the
container form — nothing else changes.

The exact commands as executed in the recorded drill — with their outputs,
exit codes and timings — are in §4; the transcript is the `transcript.txt` of the
run directory §4 names.

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
  configuration. R-LEG-2 **excludes** 22lab's database backups from the automated
  purge and requires them to be named with a manual executor instead
  (`artifacts/REQUIREMENTS.md:172`), and the erasure runbook's §3.2 is that manual
  procedure: it names the 22lab platform owner, the confirm-against-retention or
  restore-and-repurge choice, and the record to write. Until the owner fixes the
  backup's numbers, RPO cannot be stated — R-NFR-7 says only that the
  deployment's RPO/RTO must not be worsened.
- **The drill proves the procedure, not production scale.** The recorded run
  restores one 1 MiB object on a local Postgres 15.7 and MinIO; its seconds are
  not an RTO. It also does not exercise: R2's own copy/restore tools, R2 Data
  Access Logs, the production token and bucket, a dump large enough to need
  parallel restore, or a restore into a different host.
- **The legacy `FileAsset` objects.** They live in the same bucket under their
  own keys with their own rows, so the same procedure moves them; nothing here
  verifies the legacy asset surface. The regression guarantee is T-117's, and it
  is a fence with known gaps: two legacy routes are not fenced at all
  (`asset/v2.py:483`, `:915`), two of its assertions cannot fail in its fixture,
  and the legacy PATCH path is not exercised
  (`artifacts/verification/NOTE-T117-legacy-fence-coverage.md`).
- **Backup of anything else in the database** (issues, pages, SSO identities,
  etc.) is covered by the deployment's PostgreSQL backup — the same dump — but
  this runbook only verifies the file surfaces.

## 4. The recorded restore drill

Run date **2026-09-21** (`date_utc=2026-09-21T05:16:36Z`), recorded at head
`4b825f8661`, in the run directory
**`artifacts/evidence/T-121/runs/20260921T051636Z/`**. `drill.sh` writes every
artifact of one invocation into `runs/<UTC stamp>/` — the transcript (through
`tee`, so the caller sees the same text), the manifest, the pre/post inventories,
the database dump and the object copy — so a directory always describes exactly
one run and a later run lands in a new directory instead of overwriting this
one's record. The numbers below belong to the run named here; the helper scripts
(`drill.sh`, `drill_seed.py`, `drill_verify.py`) sit one level up in
`artifacts/evidence/T-121/`.

Stack: an isolated podman project (`drdrill`; `podman 6.0.0`,
`podman-compose 1.6.0`; `postgres:15.7-alpine` plus the test stack's MinIO image,
which `docker-compose-test.yml:75` names as `minio/minio` **untagged** — the
valkey and rabbitmq images beside it are pinned, MinIO is not). The API code
under test is the working tree mounted into `localhost/plane_api-tests:latest`.
The drill's inputs are the local containers, that image and the seed script; the
documentation commits that carry this runbook change none of them.

What the drill did, in order:

| Step | What happened                                                                                                                                                                                                                                                                                                       | Observed result                                                                                                                                                                                                                                                                                                                                                   |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1–2  | Started the stack, migrated the drill database with the product's own migrations                                                                                                                                                                                                                                    | both services healthy; migrations applied, exit 0                                                                                                                                                                                                                                                                                                                 |
| 3    | Seeded one file through the product's code path: workspace → project → `build_object_key` → `S3Storage.upload_file` → `file_objects` + `file_versions` rows                                                                                                                                                         | 1 MiB, sha256 `09358cb6…d107fcab`, ETag `b4790067…920354c` recorded in `manifest.json`                                                                                                                                                                                                                                                                            |
| 4    | Baseline verification (see below)                                                                                                                                                                                                                                                                                   | 5/5 checks PASS, exit 0                                                                                                                                                                                                                                                                                                                                           |
| 5    | Pre-disaster inventory: row counts, the version row (id, key, size, ETag), the full object listing as JSON                                                                                                                                                                                                          | recorded to `pre-db-rows.txt`, `pre-objects.jsonl`                                                                                                                                                                                                                                                                                                                |
| 6    | **Backup:** `pg_dump -Fc` → `backup/plane.dump` (724 591 bytes, sha256 `298c86f9…3278956` in the transcript); bucket mirrored → `backup/objects/uploads/<key>` (object sha256 `09358cb6…d107fcab`, identical to the seeded bytes); the in-container mirror was then deleted so the only copy is outside both stores | backup_elapsed **1 s**                                                                                                                                                                                                                                                                                                                                            |
| 7    | **Destroy both halves:** every object removed from the bucket; the database dropped (`DROP DATABASE plane WITH (FORCE)`) and recreated empty                                                                                                                                                                        | object listing empty; `public_tables=0`                                                                                                                                                                                                                                                                                                                           |
| 8    | **Restore the database only**, then verify                                                                                                                                                                                                                                                                          | rows present (1 file, 1 version) and the endpoint answers `200` with a signed URL, but the fetch is **HTTP 404 `NoSuchKey`** and the HEAD returns nothing → verification exits 0 only because "degraded" was the expected observation. **This is the drill's central result: a database-only restore looks healthy and is not.** restore_database_elapsed **3 s** |
| 9    | **Restore the objects**, re-run the verification with "full" expected                                                                                                                                                                                                                                               | 5/5 checks PASS: rows consistent, HEAD size matches, endpoint `200`, fetched sha256 equals the pre-disaster sha256, audit row present → total restore **5 s**                                                                                                                                                                                                     |
| 9b   | The operator command of §2 step 3, verbatim                                                                                                                                                                                                                                                                         | `live_versions=1 size_mismatches=0`                                                                                                                                                                                                                                                                                                                               |
| 10   | Post-restore inventory compared with the pre-disaster one                                                                                                                                                                                                                                                           | object identity (key, size, ETag) identical, database rows identical, table count identical — three diffs, all empty. The raw object listing differs in exactly one field: `lastModified` (the restore rewrote the object)                                                                                                                                        |
| 11   | Summary                                                                                                                                                                                                                                                                                                             | every step's exit code 0 → `DRILL RESULT: PASS`; total wall clock 83 s                                                                                                                                                                                                                                                                                            |

The two verification phases report five checks each:

1. the `file_objects` / `file_versions` rows exist and agree with the manifest;
2. the object is in the bucket, read back through `S3Storage.get_object_metadata`
   (HEAD) with the recorded size;
3. `GET …/files/{file_id}/download/` answers `200` and carries a signed URL;
4. fetching that signed URL returns `200` and a body whose sha256 equals the
   pre-disaster sha256;
5. the `downloaded` row the endpoint wrote to `file_access_logs` is present
   (the audit tail survives the disaster and the restore).

What the drill demonstrated: **§2 steps 2–4** — the dump plus object mirror, the
drop and recreate, the restore of both halves, the per-version sweep and the
fetch through the download endpoint — are executable and repeatable (the
verifier's own re-run produced the same outcomes); the two halves really are one
recovery unit (a database-only restore fails only at the fetch); a completed
restore is byte-identical at the object level and row-identical at the database
level; the download path of the restored system serves the original bytes; the
audit trail comes back with the database.

What the drill did not demonstrate, and must not be read into it: **§2 step 1**
(the freeze) — the drill stack runs no persistent API or worker, so there were no
live writers to stop, and the drill cannot show what a freeze costs or whether
the maintenance window is realistic; **§2 step 5** — the reconciliation job was
not run (it needs a Celery-less direct call, and its bucket cross-check cannot
answer against MinIO); the reverse half of the "one recovery unit" claim
(objects without rows are unreachable) is a design argument from AD-12/AD-13, not
an observation. Nothing about R2 as a provider (its own copy tools, snapshot
semantics, tokens, Data Access Logs, jurisdictional behaviour); the production
bucket or database; the backup schedule, retention or medium; a restore at
production size; a restore on a different host. The drill also did not exercise
an _upload_ through the restored system — only the read path.

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
