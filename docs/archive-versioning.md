# Archive Versioning

Archive stores logical documents in named archives. Each logical document has a mutable
`HEAD` pointer to one immutable version snapshot. A snapshot owns immutable version
metadata and one or more immutable representations; each representation owns its content
type, metadata, checksum, size, storage location, and lifecycle state. API responses never
expose storage coordinates.

## Data Model

```text
archive_documents
+ _id (PK), archive + document_id (unique)
+ head_version_id (deferred FK), lifecycle

archive_document_versions
+ _id (PK), aggregate_id (FK), version_id
+ parent_version_id (FK), document_type, metadata, created, expires
+ default_representation_id (deferred FK), snapshot_fingerprint

archive_document_representations
+ _id (PK), version_internal_id (FK), representation_id
+ name, content_type, metadata, checksum, size, state
+ backend, backend_revision, storage_key, pending_cleanup_policy

archive_idempotency
+ archive + idempotency_key (unique)
+ requested_version_id, request_fingerprint, exact response
```

Composite foreign keys ensure that `HEAD` belongs to its document, a parent belongs to the
same document, and the default representation belongs to its version. Sealed versions and
representations are immutable. Only representation cleanup state and the aggregate `HEAD`
and lifecycle may change. The archive schema is the independent Sqitch project under
`db/archive` and is deployed to a separate PostgreSQL database.

## Submission

`PUT /api/v1/archive/archives/<archive>/documents` accepts multipart data with a JSON
`manifest` field and one binary field per representation. The default archive alias is
`PUT /api/v1/archive/documents`.

```json
{
  "document_id": "00000000-0000-0000-0000-000000000001",
  "version_id": "00000000-0000-0000-0000-000000000003",
  "parent_version_id": "00000000-0000-0000-0000-000000000002",
  "created": "2030-01-01T00:00:00+00:00",
  "expires": null,
  "document_type": "invoice",
  "metadata": {"customer": "123"},
  "default_representation_id": "00000000-0000-0000-0000-000000000101",
  "representations": [
    {
      "representation_id": "00000000-0000-0000-0000-000000000101",
      "name": "original",
      "content_type": "application/pdf",
      "metadata": {},
      "field": "representation-0"
    }
  ]
}
```

Every request is one complete snapshot. Version and representation metadata are explicit;
they are not inferred from source documents. Multiple representations require explicit
representation IDs and `default_representation_id`. A single representation may omit both,
in which case the service derives its identity deterministically. The request and snapshot
fingerprints include all snapshot semantics and every representation's checksum and size.

The Archive DAG uses the same shape, except each representation contains a generic
`DocumentSource` under `source` instead of a multipart field:

```json
{
  "kind": "archive",
  "destination": "default-archive",
  "documents": [{
    "archive": "default",
    "document_id": "00000000-0000-0000-0000-000000000001",
    "version_id": "00000000-0000-0000-0000-000000000003",
    "document_type": "invoice",
    "metadata": {},
    "representations": [{
      "source": {
        "source": "generate",
        "id": "00000000-0000-0000-0000-000000000010"
      }
    }]
  }]
}
```

Omitted document and version IDs are derived deterministically where the submission is
unambiguous. Archive is synchronous: JetStream owns retries, acknowledgements, and
deterministic successor publication; Archive creates no PostgreSQL workflow-tracking state.
The HTTP adapter preserves version order for each logical document while submitting
independent documents concurrently. Its destination revision `concurrency` defaults to 10.

## Retrieval

| Operation | Scoped route |
|---|---|
| Negotiated `HEAD` content | `GET /api/v1/archive/archives/<archive>/documents/<document_id>` |
| `HEAD` details | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/details` |
| List versions | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/versions` |
| Negotiated version content | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/versions/<version_id>` |
| Version details | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/versions/<version_id>/details` |
| List `HEAD` representations | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/representations` |
| List version representations | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/versions/<version_id>/representations` |
| Exact representation | `GET /api/v1/archive/archives/<archive>/documents/<document_id>/versions/<version_id>/representations/<representation_id>` |
| Delete aggregate | `DELETE /api/v1/archive/archives/<archive>/documents/<document_id>` |

Equivalent `/api/v1/archive/documents/...` routes address the `default` archive. Unversioned
routes resolve `HEAD`; reproducible consumers pin both a version and representation.
Negotiated routes apply `Accept` across available representations and use the explicit
default when `Accept` does not select one. Responses include a representation-specific
ETag, safe `Content-Disposition`, `X-Content-Type-Options: nosniff`, and `Vary: Accept` for
negotiated downloads. Stored SHA-512 and size are verified before content is returned.

Version listing is ordered by `(created DESC, version_id DESC)` and uses opaque,
resource-bound keyset cursors. `limit` defaults to 50 and must be between 1 and 100. The
response reports `head_version_id` separately from `items`.

## Policies

Policies are selected by the destination archive; request fields cannot override them.

| Write policy | Existing changed document | Existing unchanged snapshot |
|---|---|---|
| `create-only` | `409 overwrite_rejected` | `200 unchanged` when duplicate detection is enabled |
| `versioned` | `201 version_created` | `200 unchanged` when duplicate detection is enabled |
| `replace-current` | `201 version_created` | `200 unchanged` when duplicate detection is enabled |

| History policy | Previous snapshot metadata | Previous representation content |
|---|---|---|
| `retain-all` | retained | retained |
| `retain-metadata` | retained | removed; representation becomes `metadata-only` |
| `latest-only` | retained as an immutable audit snapshot | removed; representation becomes `metadata-only` |

`require_current_predecessor` compares `parent_version_id` with locked `HEAD`.
`duplicate_unchanged: false` creates a version even when the complete snapshot fingerprint
matches. Backend revisions are immutable configuration identities and must remain configured
while their representations exist.

## Concurrency And Failure Semantics

Aggregate row locking serializes `HEAD` advancement. Exact idempotency replay returns the
recorded response; reuse of an idempotency key or requested version with different semantics
returns `version_conflict`. Representation objects are written conditionally before SQL
commit. SQL failure rolls them back; a retry may adopt an identical orphan but never
overwrite different bytes.

Deletion and history cleanup fan out one durable command per available representation.
Commands pin aggregate, version, representation, backend revision, and storage key. Missing
objects and already-completed commands are idempotent success; coordinate mismatches are
terminal-invalid; transient failures are NAKed with bounded backoff. Completion changes a
representation to `metadata-only` and clears its storage coordinates. Aggregate deletion is
final only after all available representations are gone, then versions and replay rows are
removed and a tombstone prevents identity reuse.

Representation writes are bounded by `ARCHIVE_REPRESENTATION_WRITE_CONCURRENCY`, which
defaults to 10. The deletion consumer fetches up to
`ARCHIVE_DELETION_RECONCILER_BATCH_SIZE` messages and processes at most
`ARCHIVE_DELETION_RECONCILER_CONCURRENCY` concurrently; the defaults are 100 and 25.

The DAG emits `created`, `version_created`, `unchanged`, `overwrite_rejected`, and
`version_conflict`, plus aggregate `success` for accepted submissions. Lifecycle events are
a separate at-least-once interface described in [Archive Lifecycle Events](archive-events.md).
