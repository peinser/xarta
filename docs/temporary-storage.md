# Temporary storage

Xarta stores generated scratch documents through a process-wide immutable storage backend. A document consists of a binary object and a JSON metadata object. Their stable keys are:

```text
<id[0:2]>/<id[2:4]>/<id[4:6]>/<id[6:8]>/<uuid>
<id[0:2]>/<id[2:4]>/<id[4:6]>/<id[6:8]>/<uuid>.json
```

## Configuration

Without `TMP_STORAGE_CONFIG_PATH`, the filesystem backend uses `TMP_STORAGE` as its root. This is compatible with existing deployments and files. If `TMP_STORAGE_CONFIG_PATH` is set, it must identify a JSON object. Configuration is validated when the backend is first requested and is then cached for the process lifetime. Validation performs no network calls.

Filesystem example:

```json
{"kind": "filesystem", "root": "/mnt/tmp"}
```

S3-compatible example:

```json
{
  "kind": "s3",
  "bucket": "xarta-tmp",
  "prefix": "documents",
  "endpoint_url": "https://objects.example.com",
  "region_name": "us-east-1",
  "max_pool_connections": 32,
  "aws_access_key_id": "ACCESS_KEY",
  "aws_secret_access_key": "SECRET_KEY"
}
```

The S3 backend uses the `aiobotocore` S3 API and supports AWS S3, MinIO, and SeaweedFS. `max_pool_connections` controls the process-wide HTTP connection pool and defaults to 10; configure it between 1 and 256 based on expected concurrent storage operations and backend capacity. Standard AWS credential discovery also works when credential fields are omitted. Do not log or publish configuration files containing credentials. In Helm, use `temporaryStorage.existingSecret`; the Secret must contain `temporary-storage.json`. `temporaryStorage.config` is convenient for non-secret configuration but is rendered as a Kubernetes Secret.

## Semantics

Writes are create-only. Repeating a write with identical bytes succeeds and reports that no object was created; writing different bytes to an existing key raises an error. Reads of absent filesystem or S3 objects raise `FileNotFoundError`. Deletes are idempotent.

The binary and metadata use the same shard. The binary is written first and the metadata object acts as the completion marker. A failed partial write can be retried safely because successful objects are immutable and identical writes are accepted. Retrieval requires both objects and fails if either is absent or invalid.

Cleanup removes objects older than its timezone-aware cutoff. Every invocation has an object limit. S3 listing is paginated and restricted to the configured prefix; filesystem traversal does not follow directory symlinks. Cleanup may therefore require multiple scheduled runs after a large backlog.

Configuration errors, malformed metadata, transport errors, and permission failures are propagated. S3 missing-object responses are the only transport errors normalized to `FileNotFoundError`; no secrets are included in configuration validation messages.
