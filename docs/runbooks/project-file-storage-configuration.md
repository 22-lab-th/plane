# Project file storage configuration

Instance admins can configure storage from the Storage page. Saved settings are
effective when database-backed instance configuration is enabled (`SKIP_ENV_VAR`);
otherwise the environment remains authoritative.

Cloudflare R2 uses the S3-compatible adapter. Select R2, provide its S3 access key
pair, bucket and endpoint (`https://<account-id>.r2.cloudflarestorage.com`), and use
region `auto` with signature `s3v4`. R2 selection overrides legacy `USE_MINIO=1`
endpoint rewriting. Keep the bucket private.

## Browser uploads

New project uploads sign `If-None-Match: *` as well as `Content-Type`. This makes
the object write-once: repeating a PUT after success returns 412 and cannot
replace verified bytes. The browser must send the headers returned by the
initiate-upload endpoint verbatim. Bucket CORS must allow the conditional header.
For example, substitute the actual application origin:

```json
[
  {
    "AllowedOrigins": ["https://plane.example.com"],
    "AllowedMethods": ["GET", "HEAD", "PUT"],
    "AllowedHeaders": ["Content-Type", "If-None-Match"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

Cloudflare documents support for conditional PutObject in its
[S3 compatibility reference](https://developers.cloudflare.com/r2/api/s3/api/).
MinIO integration tests verify overwrite rejection and rejection when the signed
conditional header is removed. Live R2 has not been exercised by these tests.
Previously issued upload URLs do not gain the new condition; let their original
TTL expire when deploying this change.

## Existing storage

The settings endpoint refuses provider, endpoint and bucket changes while legacy
assets, file versions or keyed exports exist, including soft-deleted records.
Credential rotation and URL expiration changes remain possible. Changing storage
identity requires a separately planned migration; this page does not move objects
or route historical files across providers. Environment changes must preserve the
same storage identity too; the UI guard cannot intercept environment edits.
