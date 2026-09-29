# Temporary storage and document references

Xarta stores intermediate documents through a process-wide immutable `TemporaryStorage` backend. The source name is **`temporary`**, regardless of whether generation, transformation, signing, bundling, UBL editing, or an upload produced the document. Temporary storage may use a shared filesystem or S3-compatible backend; it does not necessarily mean the worker's local disk.

## Document references

A reference with no `source` retrieves an existing document from temporary storage:

```json
{"id": "11111111-1111-1111-1111-111111111111"}
```

The explicit equivalent is:

```json
{"source": "temporary", "id": "11111111-1111-1111-1111-111111111111"}
```

`TemporaryDocumentSource` implements both forms. IDs must be UUIDs. There is no search across backends: a missing temporary artifact raises `FileNotFoundError`. An explicit unknown source is rejected, not replaced by the default. `null` and empty strings are not omissions. Temporary references have no archive, version, or representation fields.

Other sources remain explicit. For example, an exact archive version:

```json
{
  "source": "archive",
  "archive": "default",
  "id": "11111111-1111-1111-1111-111111111111",
  "version": "22222222-2222-2222-2222-222222222222"
}
```

`render` and `bundle` sources retain their existing on-demand production semantics. Resolving a temporary reference never executes the producer again.

## Output convention

`out` is a UUID identifying a new artifact in temporary storage. It does not select a destination, write an archive version, or modify the input document. Transform, bundle, signature, and UBL nodes use this convention. Downstream nodes use `{"id": "<out UUID>"}`. Archive storage is an explicit subsequent `archive` node.

Producer and consumer workers must share the same configured temporary backend. IDs are unique within that backend, not automatically scoped by flow or tenant. Choose new output UUIDs for new operations; reuse them only for retries of the same operation. Producers reject input/output collisions and writes of different content to occupied keys. Each producer documents how it recognizes a valid retry.

Temporary retention must cover the full workflow, retries, and operator replay window. Signing a PDF or embedding it in UBL does not automatically make either artifact durable. Archive the documents you need to retain.

## Stored representation

A document consists of a binary object and a JSON metadata object. Their stable keys are:

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

## Migration from generated-document sources

This pre-release contract replaces `source: "generate"` with `source: "temporary"`, or preferably omits `source`. `generate` remains a DAG node kind, not a retrieval source. There are no `generate` or `scratch` source aliases. Python callers use `TemporaryDocumentSource` instead of `GenerateDocumentSource`.

Signing entries now use `{"document": {"id": "<input UUID>"}, "out": "<output UUID>"}` rather than `{"in": "<input UUID>", "out": "<output UUID>"}`. Existing stored artifact bytes and keys do not change.

Update queued DAG payloads, caller examples, and profile input constraints before deploying the new workers. Publish new immutable flow-profile versions rather than editing old fingerprints. The repository's development profile advances to `development-standard@2`; the old lock entry remains as history, while version 1 is retired from the active configuration.
