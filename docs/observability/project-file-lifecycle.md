# Project-file lifecycle: records and counters

Scope: the project-file-storage feature (`file_objects` / `file_versions` /
`file_links`) and the storage it sits on. Requirement R-NFR-5, and the three counters
AC-31 names: presign, finalize and delete emit structured log records carrying the
workspace, project, file and version identifiers plus an outcome, and counters exist for
upload failures, verification mismatches and quota rejections. The fourth counter on this
page, `sweep_deletions`, is the sweep's own counter — ARCH-001 §4.4 has the sweep
increment one, and ARCH-001's R-NFR-5 row names all four — and this ticket delivers it
alongside AC-31.

Owner: the 22lab owner. This page is what an operator reads; the implementation is
`plane/utils/file_storage/observability.py` and the executable contract is
`plane/tests/contract/app/test_file_observability.py`.

## 1. The logger

Everything is written to **one logger, `plane.files`, at `INFO`**. It is registered in
`plane/settings/local.py` and `plane/settings/production.py` next to its siblings; do
not remove those entries — an unregistered logger inherits the root level (`WARNING`)
and every record below disappears silently.

Both deployments configure `pythonjsonlogger.json.JsonFormatter`, so each line is JSON
with the payload under a `file_record` (a lifecycle record) or `file_counter` (a
counter increment) key:

```json
{
  "levelname": "INFO",
  "name": "plane.files",
  "message": "file.presign outcome=signed",
  "file_record": {
    "event": "file.presign",
    "outcome": "signed",
    "workspace_id": "…",
    "project_id": "…",
    "file_id": "…",
    "version_no": 1,
    "object_key": "assets/…",
    "size_bytes": 2048,
    "mime_type": "application/pdf",
    "category": "assets",
    "expires_at": "2026-09-21T04:19:36+00:00"
  }
}
```

## 2. The record contract

Every record carries these six keys. They are keyword parameters of
`observability.record()`, so a call site cannot silently shadow them.

| Key            | Meaning                                              |
| -------------- | ---------------------------------------------------- |
| `event`        | `file.presign`, `file.finalize` or `file.delete`     |
| `outcome`      | the event's own vocabulary (below)                   |
| `workspace_id` | the workspace the file belongs to                    |
| `project_id`   | the project the file belongs to                      |
| `file_id`      | the file                                             |
| `version_no`   | the version the event concerns, or `null` (see §2.2) |

No URL, signature, token or object body is ever written: a record names a key
(`object_key`), never a signed URL (AD-15). Identifiers are strings; `null` means "not
applicable", never "unknown". The `object_key` in the sample above is abbreviated with
`…` for width — the field carries the file's exact stored key, whose canonical shape is
`workspace/{workspaceSlug}/projects/{projectStorageKey}/{category}/{entityRef}/{fileId}/v{version}/{sanitizedFilename}`,
e.g. `workspace/acme/projects/CBUTR-cbu-tracking/assets/CBUTR-11/8f1c…-uuid/v1/report.pdf`
(the `entityRef` segment is absent for a project-level file).

### 2.1 Events and outcomes

| Event           | Where it fires                                                                                              | Outcomes                                                                                                                          |
| --------------- | ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `file.presign`  | `initiate-upload/`, after the reservation commits, where the PUT URL is signed                              | `signed`, `storage_unavailable`                                                                                                   |
| `file.finalize` | `complete-upload/`: the settle, each replay branch, and each refusal that ended the attempt                 | `activated`, `superseded`, `failed` (with `code`), `replayed` (with `reason`)                                                     |
| `file.delete`   | `purge_file()`, after the commit — the `DELETE …/purge/` endpoint and the scheduled retention task share it | `purged` (with `versions`, `version_nos`, `bytes`) and `purge_failed` (with the surviving `object_key`); **both** carry `trigger` |

A request refused **before** signing (quota, permission, validation) is not a presign
outcome: it never reached the signer. The quota case is the `quota_rejections` counter
plus the `quota_rejected` audit row. A client abort is intent, not a server failure, and
emits no record here (its `upload_failed` audit row carries `reason: aborted`).

### 2.2 Why `version_no` can be `null` on a delete

`file.delete` / `purged` reports `version_no` as the file's **active pointer** at the
moment of the purge (`file_objects.current_version_no`). A file whose only attempt was
never finalised has no active version, so the pointer is `0` and the record carries
`version_no: null`. The event still names what it removed: `versions` (count) and
`version_nos` (the version numbers), so `{"version_no": null, "versions": 1,
"version_nos": [1], "bytes": 0}` reads as "one unverified version, no active one, no
accounted bytes". Alerting on `version_no: null` alone would be wrong — read
`version_nos`.

## 3. The four counters

| Counter                   | Counts exactly                                                                                                | Fires at                             |
| ------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| `upload_failures`         | a finalize refusal that ended the attempt (the server refused the object)                                     | `complete-upload/`, after the commit |
| `verification_mismatches` | the subset of those where an observed property contradicted the declaration: `size_mismatch`, `mime_mismatch` | same place                           |
| `quota_rejections`        | a ceiling refusing a request (`level`: `workspace` \| `project`)                                              | `quota.ensure_within_limits()`       |
| `sweep_deletions`         | an object deleted by the unverified-object sweep (`reservation_released` says whether it also released quota) | `sweep_version()`                    |

Deliberately **not** counted: a client abort (`upload_failures` is the server refusing an
object, not a user changing their mind), an abandoned attempt the sweep removes (that is
a `sweep_deletions`), `object_missing`/`verification_failed` (failures without a
contradiction, so they move `upload_failures` but not `verification_mismatches`), and a
replayed/idempotent request (R-NFR-6: a repeat must not move a counter).

### 3.1 How to consume them — read this before building a dashboard

- **`file_counter.value` is the counter's value _in that process_, and it is
  cumulative.** Count the `file.counter <name>=<n>` lines over your window, or sum the
  deltas between consecutive `value`s from the same process. **Never sample `value` as a
  gauge**: it only ever grows, so a poller that averages or sums samples produces a
  triangular over-count, and a worker restart makes the series jump backwards.
- **`snapshot()` is per worker**, not a fleet total. A four-worker deployment has four
  registries; only the log stream adds them up.
- **Three of the four counters are incremented after the row change they describe has
  committed** — `upload_failures` and `verification_mismatches` after a refusal at
  `complete-upload/` commits, `sweep_deletions` after the object-deleted marker commits.
  A crash between the commit and the increment loses the increment, so use the counters
  for rates and alerting and the audit row (`file_access_logs`) as the record of truth
  for a single event.
- **`quota_rejections` is the exception, and it is not 1:1 with the audit trail.** It is
  incremented inside `quota.ensure_within_limits()`, which runs inside the _caller's_
  transaction, because that is where the refusal is decided. The presign door then writes
  its `quota_rejected` audit row in that same transaction, so for presign the counter and
  the audit row agree. A refusal at the **finalize** door (the server-observed size
  crossing the ceiling) or the **copy** door returns the error and its transaction rolls
  back: the counter has already moved — the refusal did happen and was returned to the
  caller — and **no audit row exists for it**, and the version stays `uploading` with its
  reservation. Expect `quota_rejections` to exceed the `quota_rejected` audit rows
  whenever finalize or copy refusals occur; that difference is not drift.
- `file_counter` payloads carry the context of the increment (`project_id`, `file_id`,
  `code`, `level`, `reservation_released`, …), so a per-project rate needs no join.

## 4. The presign storage-call guard (AC-30)

`plane/tests/contract/app/test_file_benchmarks.py` asserts that an `initiate-upload/`
request makes **zero** calls to the storage API, by wrapping
`botocore.client.BaseClient._make_api_call` — the one method every boto3 client uses to
reach the API. Presigning signs locally and never passes through it, which is what lets
the counter distinguish "signed" from "looked at the object first".

**Scope of that guard:** it counts **botocore client calls**. Every product path builds
its client through `plane/settings/storage.py`'s `S3Storage`, so the guard holds for the
whole application; it would **not** catch a raw HTTP round trip to the storage endpoint
that bypassed botocore (a hand-rolled `requests` call, say). If such a call is ever added,
this guard goes quiet — the timing assertion in the same file is the only remaining
signal, and it is not specific about the mechanism.

## 5. Verification and falsification

`test_file_observability.py` asserts each record and each counter from the outside: the
records are read back out of `caplog` (the same `LogRecord` attributes the JSON formatter
serialises) and the counters from `snapshot()`. Every guard was falsified by replacing the
emission site with a no-op (`artifacts/evidence/T-120/05-falsification-noop-sites.log`
names the test that fails for each), the presign guard by injecting a lookup before
signing, and the list benchmark by removing the seed's `ANALYZE`.

That last falsification is worth reading carefully, because how visible it is depends on
the database's state: against a **cold** test database the same 10 000-row list request
reads **p95 3578 ms** and fails the 400 ms budget loudly, while a **reused** database
(`--reuse-db`, which is this suite's default) carries the previous run's statistics and
masks the effect. So the `ANALYZE` in the benchmark's seed is not optional, and when it is
missing the failure is a planner-statistics effect of the seed rather than a property of
the endpoint.
