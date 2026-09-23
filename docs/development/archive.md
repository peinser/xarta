# Archive Development

The devcontainer configures two logical archives backed by separate MinIO buckets.
Archive relational state is stored in the separate `archive` PostgreSQL database on
the same local `postgres` server. `make db-deploy` creates and migrates it.

| Archive | Backend | Bucket | Write policy | History policy | Predecessor required |
|---|---|---|---|---|---|
| `default` | `default-s3@v1` | `xarta-archive` | `versioned` | `retain-all` | yes |
| `secondary` | `secondary-s3@v1` | `xarta-archive-secondary` | `versioned` | `retain-metadata` | no |

Both backends use the local `http://minio:9000` endpoint and development-only
credentials from [`.dev/compose.yml`](../../.dev/compose.yml). The complete registry
is in [`.dev/conf/archive-storage.json`](../../.dev/conf/archive-storage.json).

The two archives deliberately exercise different behavior:

- `default` keeps every immutable snapshot and representation. A changed version must
  identify `HEAD` through `parent_version_id`.
- `secondary` permits a changed version without an explicit predecessor. When the
  JetStream deletion consumer processes a superseded version, each representation's content
  is removed while its details remain available with state `metadata-only`.

The `minio_init` Compose service creates both buckets. Existing environments can
provision a newly added bucket without restarting MinIO:

```console
docker compose -f .dev/compose.yml up --no-deps --force-recreate minio_init
```

The archive service and expiration job use `ARCHIVE_POSTGRESQL_USER`,
`ARCHIVE_POSTGRESQL_PASSWORD`, `ARCHIVE_POSTGRESQL_DATABASE`, and
`ARCHIVE_POSTGRESQL_HOST`. Other services continue to use `POSTGRESQL_*`.

Archive registry configuration is loaded when Xarta starts, so restart `make
standalone` or `make debug` after changing it. MinIO itself does not need a restart.

## Verification

Run the focused archive, lifecycle, and tracking test suite with:

```console
make archive-test
```

The suite covers archive workflow submission, version policies, immutable storage,
idempotency, rollback, history cleanup, safe deletion, expiration, reconciliation,
desired-state recovery, event publication, retries, and tracked execution behavior.

Useful live endpoints are:

```text
PUT    /api/v1/archive/archives/{archive}/documents
GET    /api/v1/archive/archives/{archive}/documents/{document_id}
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/details
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/versions
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/versions/{version_id}
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/versions/{version_id}/details
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/versions/{version_id}/representations
GET    /api/v1/archive/archives/{archive}/documents/{document_id}/versions/{version_id}/representations/{representation_id}
DELETE /api/v1/archive/archives/{archive}/documents/{document_id}
```

`DELETE` is safe by default and only accepts an expired `HEAD` version. Use
`?safe=false` only for an explicit administrative deletion test. Deletion and
history-content removal are asynchronous and carried by durable messages in the
`ARCHIVE_COMMANDS` JetStream work queue. Persisted deletion intent is scanned on
startup and every `ARCHIVE_DELETION_RECONCILER_SCAN_SECONDS` while pending.

Archive lifecycle events are published on `documents.archive.>` and retained in
the `DOCUMENTS` JetStream stream. The implemented event types are `created`,
`version-created`, `deletion-requested`, `deleted`, and
`version-content-removed`.

The version collection uses opaque cursor pagination with a default limit of 50
and a maximum of 100. The `latest-only` policy currently removes historical
content in the same way as `retain-metadata`; metadata-row pruning and the reserved
`history-pruned` event are not implemented.
