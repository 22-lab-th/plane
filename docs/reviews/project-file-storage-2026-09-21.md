# Project file storage review — 2026-09-21

Scope: `feat/project-file-storage` against `origin/preview` at
`a5be3c1672b8ad8838a2cd8a79d4865e709441de`, including the folder management and
instance storage configuration changes in the working tree. This is a review
snapshot, not a release approval. The findings below remain open.

## Standards and frontend behavior

No non-tooling violation of the documented repository standards was established.

- **P2 — Pagination is not exposed.** `apps/web/core/components/files/root.tsx`
  renders the first `results` response but never consumes `page.next_cursor`.
  The API defaults to 50 files. Matching files beyond that page cannot be reached
  through browsing. Add navigation or incremental loading and a >50-file test.
- **P2 — Folder destinations are ambiguous.**
  `apps/web/core/components/files/folder-management.tsx` shows only names and
  depth, sorted across the entire tree. `Engineering/Archive` and
  `Finance/Archive` appear identical. Show the parent path or a nested tree.
- **P2 — Storage settings accept invalid addressing styles.**
  `apps/admin/app/(all)/(dashboard)/storage/form.tsx` uses a free-text field;
  the generic configuration endpoint persists it without a provider-specific
  allowlist. A typo is accepted, then boto3 client construction fails. Validate
  on the server and use a select in the form.
- **P2 — Static credentials are mandatory for S3.** The storage form requires
  both keys even for installations using ambient IAM credentials. Allow the
  credential-provider chain for S3 and validate explicitly supplied pairs.

## Specification and backend behavior

- **P1 — PUT URLs can overwrite verified bytes.**
  `apps/api/plane/settings/storage.py::generate_presigned_put` signs the key and
  MIME but has no write-once condition. Finalize publishes that same key. Reusing
  the URL before expiration replaces verified bytes without verification,
  quota settlement or an audit event. Enforce immutable writes or publish a
  separate verified object. S3 documents both URL reuse and replacement of an
  existing object: https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html
- **P2 — Failed copy cleanup can lose ownership of objects.**
  `apps/api/plane/app/views/file/operations.py` rolls target rows back, then
  attempts best-effort object deletion. Failed cleanup is only logged, leaving
  objects undiscoverable by the database-driven sweep. A timed-out copy can also
  succeed remotely without entering `copied_keys`. Persist ownership of every
  attempted destination key and retain failed cleanup records.
- **P1 — Purge has no single owner.**
  `apps/api/plane/utils/file_storage/purge.py::purge_file` reads versions and
  deletes objects before acquiring its counter locks. Concurrent manual and
  scheduled purges can both subtract cached byte totals. Restore is likewise
  uncoordinated and can report success while purge deletes the objects. Claim
  and revalidate the file before external effects and test competing operations.
- **P1 — R2 selection does not override MinIO routing.**
  `apps/api/plane/settings/storage.py::__init__` still branches on `USE_MINIO=1`
  without checking the saved provider. With R2 selected, this can replace the
  R2 endpoint with the request host. The export task retains the same MinIO
  mode decision. An isolated execution of the actual constructor with mocked
  boto3 reproduced routing to the application host instead of the saved R2 URL.
- **P1 — Changing storage identity strands existing files.** The new saved
  configuration changes the global endpoint and bucket immediately. Existing
  download and purge callers pass object keys without selecting the original
  storage identity. Switching buckets therefore breaks downloads; a successful
  delete of a missing key in the new bucket can remove database records while
  leaving the original bytes behind. Preserve per-object storage identity or
  reject storage identity changes while existing objects need that backend.

## Validation

- Admin and Web typechecks: passed.
- Web unit tests: 10 passed in 3 files.
- Changed TypeScript files: project sidebar shadowing and mutable sort warnings
  were corrected to satisfy the warning-free pre-commit hook; formatting passed for all 19 changed TS/TSX/JSON files.
- Focused backend storage/upload/copy/trash/purge suite: 102 passed, using the
  isolated `plane-review` Podman Compose test project.
- Broader backend project file contract and unit suite: 399 passed in 92.22 s
  (1,351 dependency deprecation warnings). This includes listing benchmarks,
  legacy assets, cross-project operations, folders, quota, retention and versions.
- Existing tests passing do not resolve the findings above; they do not cover
  these adversarial and concurrent scenarios.
- No live R2 credentials or production services were exercised.

## Integration status

The remote's default branch is `preview`; `main` does not exist. The requested
merge destination needs clarification. Do not treat this snapshot as ready to
merge while the P1 findings remain open.
