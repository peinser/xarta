# Archive Capability

The `archive` capability stores complete, immutable document snapshots in named
archives. A snapshot can contain several representations of the same logical
document, such as a PDF for people and an XML file for software. Clients can retrieve
the current version, pin a historical version and representation, inspect metadata,
and request retention-aware deletion.

This guide covers the implemented workflow capability, HTTP API, storage policies,
and operational behavior. The machine-readable HTTP and workflow schemas are in
the [v1 OpenAPI contract](../contracts/openapi/v1/openapi.json). For additional
detail, see [Archive versioning](archive-versioning.md),
[Archive lifecycle events](archive-events.md), and
[Archive development](development/archive.md).

## Contents

- [Concepts and architecture](#concepts-and-architecture)
- [Using archive in a workflow](#using-archive-in-a-workflow)
- [Writing snapshots over HTTP](#writing-snapshots-over-http)
- [Version policies and idempotency](#version-policies-and-idempotency)
- [Retrieval and document sources](#retrieval-and-document-sources)
- [Retention and deletion](#retention-and-deletion)
- [Lifecycle events](#lifecycle-events)
- [Configuration and deployment](#configuration-and-deployment)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Implementation map](#implementation-map)

## Concepts and architecture

| Concept | Meaning |
|---|---|
| Destination | A server-configured outbound adapter binding, selected by the workflow node. It determines which archive HTTP service receives the write. |
| Archive | A named logical namespace on that service, with its own storage backend and policies. |
| Document | A logical identity, unique within an archive, identified by `document_id`. |
| Version | A complete immutable snapshot of the document, identified by `version_id`. |
| `HEAD` | The document's mutable pointer to its current version. It advances when a new snapshot is accepted. |
| Representation | One immutable binary object within a version, with its own identity, media type, metadata, SHA-512 checksum, size, and lifecycle state. |
| Default representation | The representation selected when a download or document source does not choose another one. |
| Backend revision | The configured storage location used for a representation. Its identity is retained so later reads and cleanup use the original location. |

A destination name and an archive name are different settings. For example,
`default-archive` can select an HTTP adapter whose requests target the logical archive
`default`. Two archives may contain the same document UUID without referring to the
same logical document.

```text
intake -> JetStream archive task -> archive worker
                                     |
                                     | resolve sources and destination
                                     v
                             xarta-http-archive adapter
                                     |
                                     | multipart snapshot PUT
                                     v
                             archive HTTP service
                               /             \
                     archive PostgreSQL    filesystem or S3
                               |
                               +-> lifecycle events -> DOCUMENTS
                               +-> cleanup commands -> ARCHIVE_COMMANDS
                                                        |
                                                        v
                                                 deletion consumer
```

The current outbound adapter, `xarta-http-archive`, is **synchronous**: it knows the
write result before the task handler returns. JetStream owns workflow delivery,
retries, acknowledgements, and deterministic successor publication. Archive does
not create PostgreSQL workflow-tracking records for the node. Its archive database
stores document state, replay records, and deletion intent, which are separate
from workflow execution tracking.

An accepted archive write does not wait for superseded content to be removed.
History cleanup and document deletion run asynchronously.

## Using archive in a workflow

### Minimal node

This node archives an existing immutable temporary artifact. Replace the source
UUID with an artifact produced earlier in the flow or uploaded to temporary storage.
The destination must exist in the worker's configuration.

```json
{
  "kind": "archive",
  "destination": "default-archive",
  "documents": [
    {
      "archive": "default",
      "document_id": "10000000-0000-0000-0000-000000000001",
      "metadata": {"customer": "123"},
      "representations": [
        {
          "name": "invoice.pdf",
          "source": {
            "source": "temporary",
            "id": "20000000-0000-0000-0000-000000000001"
          }
        }
      ]
    }
  ],
  "on": {
    "success": [{"kind": "debug"}],
    "failure": [{"kind": "debug"}],
    "execution_failed": [{"kind": "debug"}]
  }
}
```

This is a node fragment, not the complete intake request. See the complete
[destination example](../examples/protocol/archive-destination.json) and
[outcome-routing example](../examples/protocol/archive-destination-events.json)
for request envelopes.

### Node and snapshot fields

The node contains `kind: "archive"`, an optional `destination`, and `documents`, an
array of snapshot specifications. Each entry is one version submission; several
entries may address the same logical document.

| Snapshot field | Behavior |
|---|---|
| `archive` | Optional archive name. Overrides the destination revision's `archive`; otherwise defaults to `default`. Names match `[A-Za-z0-9][A-Za-z0-9._-]*`. |
| `document_id` | Optional UUID. Supply it when the logical identity must be known outside this node execution or when updating an existing document. |
| `version_id` | Optional UUID for this snapshot. A version is immutable; changing a snapshot requires a new identity. |
| `parent_version_id` | Optional expected current version. If supplied, it must match locked `HEAD`. Some archives require it for updates. |
| `created` | Optional timezone-aware ISO 8601 timestamp. Defaults to the task's creation time. |
| `expires` | Optional timezone-aware timestamp, later than `created`. Document-type retention can supply a default when omitted. |
| `document_type` | Optional nonempty document-type identifier, validated by the receiving service against its document-type configuration. |
| `metadata` | Explicit version-level JSON object; defaults to `{}`. |
| `representations` | Nonempty array of representation specifications. Every request supplies the whole snapshot. |
| `default_representation_id` | Required for several representations; must identify a representation in this snapshot. Inferred for a single representation. |

Each workflow representation contains:

| Field | Behavior |
|---|---|
| `source` | Required generic document-source object. Supported source kinds are (at the time of writing) `temporary`, `archive`, `render`, and `bundle`. |
| `representation_id` | UUID, required explicitly when a workflow snapshot has several representations. |
| `name` | Optional nonblank name, used as the download filename. The receiving service rejects control characters. |
| `metadata` | Explicit representation-level JSON object; defaults to `{}`. |

The worker takes **bytes and content type** from the retrieved source. It does not
inherit source metadata, document type, creation time, or expiration into the new
snapshot. Declare the intended version and representation metadata explicitly.

### Identity and repeatability

Omitted workflow IDs are UUIDv5 values derived from `node_execution_id` and the
document/representation positions. The task creation time supplies a stable
`created` value. This keeps identity and time stable on redelivery of the same
execution; it does not make a new execution the same document automatically.

For repeatable writes, use immutable source artifacts or pinned archive sources.
A `render` source may produce different bytes when re-run, and an unpinned archive
source may resolve a newer `HEAD`. Repeating the same write identity with different
bytes or semantics conflicts rather than overwriting the original. See
[temporary storage](temporary-storage.md) for immutable intermediate artifacts.

### Ordering, partial success, and outcomes

The adapter groups submissions by `(archive, document_id)`. Within each group it
submits versions in input order; independent groups run concurrently, with a
default concurrency of 10. The node is **not a multi-document transaction**.
Earlier successful writes remain stored if another submission is rejected or a
later request fails. A semantic conflict does not automatically stop subsequent
submissions in the same group.

For each completed document submission, the worker emits one semantic outcome
with subject `kind: "document"`, subject ID equal to `document_id`, and
`details.version_id` equal to the resolved version:

| Outcome | Meaning |
|---|---|
| `created` | The first snapshot was accepted for the document. |
| `version_created` | A new snapshot was accepted and became `HEAD`. |
| `unchanged` | The complete snapshot is equivalent to current `HEAD`; no version was created. The emitted version ID is the existing version. |
| `overwrite_rejected` | The archive's `create-only` policy rejected a changed document. |
| `version_conflict` | Identity, predecessor, or another archive conflict prevented the submission. |

It also emits aggregate `success` when there are no semantic rejection errors, or
aggregate `failure` when any submission was rejected. A mixed batch can therefore
emit successful document outcomes as well as aggregate `failure`. An exact replay
retains its original semantic outcome; replay is not a separate DAG outcome.

Transport and execution failures are distinct from semantic rejection. Timeouts,
connection/payload failures, HTTP `408`, `425`, `429`, and `5xx` responses become
retryable transport errors. The worker requests automatic replay with an initial
5-second delay and backoff multiplier 2. Other unsuccessful statuses and malformed
successful responses raise execution errors and follow the common runtime failure
handling. See [tracked capabilities](tracked-capabilities.md) and
[dead-letter handling](dead-letter-handling.md) for runtime terminal outcomes such
as `execution_failed` and `retry_exhausted`.

## Writing snapshots over HTTP

Use:

```text
PUT /api/v1/archive/archives/{archive}/documents
```

`PUT /api/v1/archive/documents` is the equivalent route for the archive named
`default`. Floating `/api/archive/...` aliases also exist; use `/api/v1/archive/...`
for a pinned API version.

The request is `multipart/form-data` with a JSON **text field** named `manifest`
and a binary upload field for every representation. `field` in the manifest binds
each representation to its upload. Supply a bare media type such as
`application/pdf`; the manifest validator does not accept media-type parameters
such as `; charset=utf-8`.

### Complete two-representation example

Save this JSON as `manifest.json`. The files `invoice.pdf` and `invoice.xml` must
already exist. `document_type` is omitted so the example does not depend on a
particular deployment's document-type registry.

```json
{
  "id": "10000000-0000-0000-0000-000000000001",
  "version": {
    "id": "30000000-0000-0000-0000-000000000001"
  },
  "created_at": "2026-10-14T12:00:00+00:00",
  "expires_at": "2036-10-14T12:00:00+00:00",
  "metadata": {
    "customer": "123",
    "invoice_number": "INV-001"
  },
  "representations": {
    "default": "40000000-0000-0000-0000-000000000001",
    "items": [
      {
        "id": "40000000-0000-0000-0000-000000000001",
        "field": "pdf",
        "name": "invoice.pdf",
        "metadata": {
          "purpose": "human-readable"
        },
        "content": {
          "type": "application/pdf"
        }
      },
      {
        "id": "40000000-0000-0000-0000-000000000002",
        "field": "xml",
        "name": "invoice.xml",
        "metadata": {
          "purpose": "machine-readable"
        },
        "content": {
          "type": "application/xml"
        }
      }
    ]
  }
}
```

```sh
ARCHIVE_API='http://localhost:8000/api/v1/archive'

curl --fail-with-body --request PUT \
  "$ARCHIVE_API/archives/default/documents" \
  --header 'Idempotency-Key: invoice-001-v1' \
  --form 'manifest=<manifest.json;type=application/json' \
  --form 'pdf=@invoice.pdf;type=application/pdf' \
  --form 'xml=@invoice.xml;type=application/xml'
```

The `<manifest.json` form syntax sends the manifest as a text field, while `@`
sends a file upload. Adjust the origin to the deployed service or gateway.

HTTP representations use `field` and `content.type` instead of a workflow `source`.
Upload fields and representation IDs must be unique within the snapshot. A single
representation can omit its identity and the default identity. For multiple
representations, supply explicit identities and the default identity, as above.
The HTTP parser can derive omitted representation IDs, but the workflow parser
requires them explicitly for multi-representation snapshots.

HTTP manifests identify documents with `id`, snapshots with `version: {id, parent}`,
and representations with `representations: {default, items}`. Each upload item has
an optional `id`, optional `name`/`metadata`, a required multipart `field`, and
`content: {type}`. The service computes checksums and sizes; clients cannot submit
those properties or content availability. Unknown fields are rejected, including
obsolete flat identity fields. Metadata maps remain free-form.

### Responses

| HTTP status | Meaning |
|---|---|
| `201` | Newly created document or version. |
| `200` | Equivalent current snapshot (`unchanged`) or exact replay. |
| `400` | Invalid manifest or fields, such as missing uploads or invalid timestamps. |
| `404` | Unknown destination archive. |
| `409` | Conflict, with `error.code` and explanatory `error.message`. |

A successful response contains only `archive`, logical document `id`, resolved
`version`, and `outcome`:

```json
{
  "archive": "default",
  "id": "10000000-0000-0000-0000-000000000001",
  "version": {
    "id": "30000000-0000-0000-0000-000000000001",
    "parent": null
  },
  "outcome": "created"
}
```

The version is an object with `id` and `parent`; `parent` is the preceding version
UUID or `null` for the first snapshot. It describes the stored version, including
when an equivalent snapshot is returned instead of the requested new version.
Use the details or representations endpoint for generated representation IDs,
metadata, server-computed checksums, and byte sizes.
An exact retry returns the original operation's outcome and identities with HTTP
`200`; it does not create another version or expose an internal replay flag.

HTTP conflicts include `overwrite_rejected`, `version_conflict`,
`deletion_in_progress`, and `document_deleted`. The outbound workflow adapter
normalizes the latter two to its supported `version_conflict` outcome.

Errors from matched archive routes use a consistent JSON envelope, for example:

```json
{
  "error": {
    "code": "version_conflict",
    "message": "parent version does not match HEAD"
  }
}
```

Validation, missing resources, retention failures, and unexpected errors likewise
use `error.code`/`error.message`. Gateway failures or errors before a route is
matched are outside this handler. HTTP `300` selection ambiguity additionally
returns `representation_ids`, an array of UUID references rather than objects.

The archive routes do not enforce application-level authentication themselves.
Access controls are deployment-dependent, such as gateway, ingress, and network
restrictions. In particular, an archive name is a namespace, not an authorization
boundary. The current outbound adapter sends `Idempotency-Key` but has no
configured authorization-header option.

## Version policies and idempotency

### Complete snapshots and predecessor checks

A write is never a metadata patch or an append to a representation collection.
To change metadata, expiration, bytes, names, or representation membership, submit
a complete new version.

The service locks the logical document while checking and advancing `HEAD`.
Whenever HTTP `version.parent` (`parent_version_id` in a workflow node) is supplied,
it must match current `HEAD`, even when
the archive does not require a predecessor. With
`require_current_predecessor: true`, every update must supply it. This check occurs
before equivalent-snapshot detection: an unchanged submission with a stale or
missing required parent can still conflict.

To update the example HTTP document, use a new `version.id` and idempotency key,
include all representations, and set `version.parent` to the current version ID. On a
conflict, retrieve `HEAD` again and decide whether the intended update is still
valid. When a predecessor is optional and omitted, the stored parent is still the
actual previous `HEAD`.

`HEAD` is the latest accepted version, not necessarily the version with the latest
caller-supplied `created_at` timestamp.

### Write and history policies

Policies belong to the receiving archive configuration. Requests cannot override
them.

| Write policy | Changed snapshot for an existing document |
|---|---|
| `create-only` | Rejected with `overwrite_rejected`. |
| `versioned` | Creates an immutable version and advances `HEAD`. |
| `replace-current` | Currently also creates an immutable version and advances `HEAD`; it does not mutate existing bytes. |

With `duplicate_unchanged: true` (the default), an equivalent snapshot returns
`unchanged` under any write policy after predecessor validation. With the flag
disabled, equivalent submissions proceed through the write policy: versioned
archives can create another version, while `create-only` rejects an existing
document.

| History policy | Superseded version metadata | Superseded representation bytes |
|---|---|---|
| `retain-all` | Retained | Retained |
| `retain-metadata` | Retained | Removed asynchronously |
| `latest-only` | Currently retained | Removed asynchronously |

Removed representations become `metadata-only`. `latest-only` currently behaves
like `retain-metadata` for content cleanup; metadata-row pruning and the reserved
`history-pruned` event are not implemented. Write policy and history policy are
independent: `replace-current` alone does not remove older content.

### Exact replay versus equivalent content

These are separate decisions:

1. **Exact replay** checks the request fingerprint against saved idempotency and
   requested-version records. Reusing an identity with different request semantics
   produces `version_conflict`.
2. **Equivalent snapshot detection** compares the submitted snapshot with current
   `HEAD`. It includes expiration, document type, version metadata, each
   representation's name/media type/metadata/checksum/size, and the default
   representation's semantics. It excludes creation time, predecessor, and the
   requested version and representation identities.

Consequently, a new requested version ID does not by itself force a new version.
When content is unchanged, use the returned version ID rather than assuming the
requested one was stored. Changing `created_at` alone also does not force a new
version, although a retention default derived from that timestamp can change
`expires_at` and therefore the snapshot fingerprint.

HTTP idempotency keys are scoped to an archive. When supplying `Idempotency-Key`,
the manifest must include a stable `created_at`. Omitted HTTP document and version
IDs derive deterministically from the archive, key, and document identity. Without
a supplied key, the service generates a fresh key and defaults `created_at` to now;
clients should not rely on that mode for retryable creation.

The workflow adapter sends
`<node_execution_id>:<document_id>:<version_id>` as the per-request key. Replay
records have no time-based expiry in the current implementation; aggregate deletion
removes them and leaves a tombstone that prevents reuse of the document identity.

### Storage failure boundary

Representations are written before SQL commit using immutable, conditional writes.
Filesystem storage publishes with a hard link; S3 uses `If-None-Match: *`.
Each parsed snapshot gets a fresh, server-generated allocation UUID. Each physical
filename ends with `{representation_id}.{allocation_id}` within its backend's key
layout. A different request cannot adopt that attempt's allocations. A low-level
retry of the same physical PUT can still recognize identical bytes at that exact
key; different bytes at the same key are never overwritten.

The allocation UUID is independent of the logical document/version/representation
IDs and semantic idempotency key. It is excluded from both semantic fingerprints.
A committed replay is resolved before persistence, so its newly prepared private
keys do not result in new objects.

Compensation runs after the database transaction has exited and released its lock.
Attempt-private physical keys prevent a delayed cleanup from deleting content
another attempt has adopted and committed:

```text
A writes K_A, then its database transaction rolls back.
B writes K_B and commits a reference to K_B.
A compensates by deleting only K_A; K_B remains intact.
```

If work fails before the transaction body finishes, the service attempts to remove
objects created by that attempt. A commit error after the transaction body finishes
is ambiguous: PostgreSQL may already reference the objects, so the service keeps
them rather than risking data loss. Rollback failures or process crashes can also
leave orphan objects. Later submissions use new private allocations rather than
adopting those orphans. Reclaiming unreferenced content is a separate operation
which must protect in-flight/uncertain writes as well as check committed references;
the absence of a visible database row alone does not make deletion safe. Retry with the same
logical identities and stable content; do not treat an HTTP timeout as proof that
nothing was stored. Private ownership does not make an ambiguous commit safe to
compensate.

## Retrieval and document sources

### Authoritative metadata search

`POST /api/v1/archive/archives/{archive}/documents/search` (or the default archive
alias `/api/v1/archive/documents/search`) provides read-only, cursor-based lookup
of logical metadata on active HEADs. All archived documents are eligible without
a `search-index` node. It searches neither historical nor representation metadata.
See [metadata search](archive-metadata-search.md) for the JSON filter contract,
examples, live traversal semantics and required Sqitch index upgrade.

### HTTP routes

All paths below are relative to `/api/v1/archive/archives/{archive}`. Equivalent
routes without `/archives/{archive}` address `default`.

| Method | Path | Result |
|---|---|---|
| `GET` | `/documents/{document_id}` | Negotiated `HEAD` content |
| `GET` | `/documents/{document_id}/details` | `HEAD` version metadata and representations |
| `GET` | `/documents/{document_id}/representations` | `HEAD` representation collection |
| `GET` | `/documents/{document_id}/versions` | Paginated version history |
| `GET` | `/documents/{document_id}/versions/{version_id}` | Negotiated version content |
| `GET` | `/documents/{document_id}/versions/{version_id}/details` | Exact version metadata |
| `GET` | `/documents/{document_id}/versions/{version_id}/representations` | Exact version's representation collection |
| `GET` | `/documents/{document_id}/versions/{version_id}/representations/{representation_id}` | Exact representation bytes |
| `DELETE` | `/documents/{document_id}` | Aggregate deletion request |

Details, write responses, and collection envelopes use **`id`** for the logical
document UUID. Details, representation collections, and write responses group
snapshot identity as **`version: {"id": "<version UUID>", "parent": null}`**;
`parent` is the preceding version UUID when one exists, not an embedded object.
Details also contain `archive`, `created_at`, `expires_at`, `document_type`,
`metadata`, `content_availability`, and `representations: {default, items}`.
Each representation has `id`, `name`, `metadata`, and
`content: {type, size_bytes, checksum, availability}`. The checksum is an object
with a `sha512` hexadecimal digest. Storage paths, bucket names,
backend identities, and credentials are not exposed.

`content.availability` is `available` while that representation's bytes are
recorded as retained, or `metadata-only` after removal. It is not workflow progress,
a live storage-health check, or an expiry check. The snapshot's aggregate
`content_availability` remains `available` while **any** representation retains
bytes; the default may already be metadata-only during cleanup. Sizes and checksums
continue to describe the original bytes after removal.

### Content negotiation and integrity

Negotiated routes select among available representations:

- Missing `Accept`, or exactly `*/*`, selects the default representation.
- If the default matches an accepted media range with positive quality, it wins.
- Otherwise the server chooses the best-ranked matching representation.
- Multiple equally ranked candidates return `300` with their representation IDs.
- No accepted representation returns `406`.

Use the exact representation route to resolve ambiguity; it bypasses negotiation.
It also makes the selected bytes explicit for reproducible consumers.

Before returning content, the service reads it and verifies its length and SHA-512
against the stored values. A mismatch is a server error. A missing object or
unconfigured backend revision is reported as unavailable content. Successful
downloads include `Content-Type`, `Content-Length`, safe attachment
`Content-Disposition`, `X-Content-Type-Options: nosniff`, and a checksum-based ETag.
Negotiated responses include `Vary: Accept`. The handler emits an ETag but does not
implement conditional `304` responses or byte-range downloads.

### Version pagination

```text
GET /api/v1/archive/archives/default/documents/{document_id}/versions?limit=50
```

The response includes `archive`, `id`, `head_version_id`, `items`, and
`pagination: {limit, next}`. Pass a non-null `pagination.next` unchanged as the
next request's `cursor` query parameter. It is an opaque token, not a URL or a page
number. `next: null` ends traversal. `pagination.limit` is the effective maximum
batch size, not the number of items returned, including on an empty final batch.
Each item uses the same nested `version` shape as a details response.
The default limit is 50; the allowed range is 1–100. Cursors are opaque and bound
to the archive/document collection; malformed or cross-resource cursors are
rejected.

Ordering is `(created_at DESC, version.id DESC)`. Use `head_version_id` to identify
current `HEAD`, rather than assuming the first history item is current. A cursor
does not freeze the collection while concurrent writes occur.

### Using archived content in another capability

HTTP manifests use the nested identity and representation shapes described above.
The executable workflow-node protocol still uses `document_id`, `version_id`,
`parent_version_id`, `created`, `expires`, `default_representation_id`, and a list
of source-backed representations; the HTTP adapter maps those inputs to the upload
manifest. Generic document source references use scalar `version` and
`representation` UUIDs (the HTTP response's nested `version` is not a source reference):

```json
{
  "source": "archive",
  "archive": "default",
  "id": "10000000-0000-0000-0000-000000000001",
  "version": "30000000-0000-0000-0000-000000000001",
  "representation": "40000000-0000-0000-0000-000000000001"
}
```

Omitting `archive` selects `default`; omitting `version` resolves current `HEAD`;
omitting `representation` selects that version's default representation. An exact
representation requires a version. Pin both for reproducible downstream work.
Sources resolve through `ARCHIVE_SERVICE_ENDPOINT`, not through the outbound
node's destination selection.

The separate `wait-for` archive handler checks active document existence in the
local archive database's `default` archive. It does not verify a pinned version,
representation, or remote outbound destination, so it is not a general readiness
check for every archive source.

## Retention and deletion

### Expiration

An explicit HTTP `expires_at` must be later than `created_at`. Both timestamps
include a timezone. `created_at` is caller-supplied snapshot creation time, not
necessarily the service ingestion time. If a document type is supplied,
the receiver looks it up; when expiration is omitted and the type has
`default_retention`, expiration becomes `created_at + default_retention`.
Without either an explicit expiration or such a default, expiration remains null.

Expiration makes a document eligible for safe aggregate deletion. It is not an
automatic read-time access cutoff: content remains readable while the aggregate
is active. The expiration job selects expired current documents and requests safe
deletion through the archive API. An expired historical version does not by itself
expire the aggregate; the current `HEAD` controls safe deletion.

The `expire-archived-documents` job processes at most `CLEANUP_BATCHSIZE` documents
per run (default 10,000), with up to 32 concurrent deletion requests. Its schedule
is configured by the deployment. History cleanup after a new version is a separate
policy mechanism and does not wait for the old version's expiration.

### Deletion lifecycle

```text
active -> deleting -> deleted tombstone
```

By default, `DELETE` uses `safe=true`:

- A document without expiration cannot be safely deleted.
- A document whose `HEAD` has not expired is rejected.
- `?safe=false` bypasses those retention checks and requests administrative deletion.

`safe` accepts case-insensitive `true/1/yes` and `false/0/no`. Invalid values are
bad requests. Acceptance returns empty `202`; a missing document or already-deleted
tombstone returns empty `200`. A repeated request while deletion is pending can
return `202` again. `202` does not mean all bytes have already been removed.

Deletion is for the **whole document**, including every version and representation.
Once marked `deleting`, normal read/detail/list queries no longer expose it. New
writes are rejected. The consumer removes available representation content;
finalization removes version, representation, and replay rows while retaining the
document tombstone. The same archive/document identity cannot be recreated.

### Durable cleanup and recovery

Deletion and history retention create one command per available representation on
`archive.commands.delete`, in the `ARCHIVE_COMMANDS` JetStream work queue. Commands
pin internal and public document/version/representation identities, backend revision,
and storage key. The consumer validates these coordinates before removing content.

Missing objects and already-completed work are idempotent success. Coordinate
mismatches are terminal-invalid; transient failures are negatively acknowledged
with bounded backoff. Completed history cleanup leaves metadata and clears storage
coordinates. Document deletion finishes only after all available content is gone.

Persisted deletion and history-cleanup intent is scanned on startup and periodically
to recover commands lost between SQL commit and broker publication. This recovery
is for desired cleanup state, not a general lifecycle-event outbox.

## Lifecycle events

Lifecycle events are separate from workflow outcomes. They are published directly
to the `DOCUMENTS` stream on:

```text
documents.archive.<archive-token>.<document-uuid>.<event-token>
```

The archive token is unpadded base64url of the archive name. Implemented event types
are:

| Event type | Meaning |
|---|---|
| `xarta.archive.document.created` | First snapshot accepted |
| `xarta.archive.document.version-created` | Subsequent snapshot accepted |
| `xarta.archive.document.deletion-requested` | Aggregate deletion requested |
| `xarta.archive.document.deleted` | Aggregate deletion finalized |
| `xarta.archive.document.version-content-removed` | One representation's content removed |

Events use a CloudEvents-style JSON envelope and deterministic event IDs, also
supplied as `Nats-Msg-Id`. Consumers must tolerate duplicates and must not infer
business ordering from broker sequence alone. An `unchanged` submission does not
create a new creation/version event; exact replay of a creation write republishes
its deterministic fact.

Publication occurs after SQL commit, without an archive event outbox. A crash
between commit and publication can lose an event; a replay can repair a creation
event, and retry after broker acceptance can duplicate it outside the broker's
deduplication window. Do not assume that stream delivery guarantees make the
database-to-event handoff lossless.

Events omit content and storage coordinates, but include application metadata.
See [Archive lifecycle events](archive-events.md) for the envelope and consumer
contract. Search indexing is a separate concern; see
[Search indexing](search-indexing.md).

## Configuration and deployment

### Outbound destinations

`ARCHIVE_CONFIGURATIONS_CONFIG_PATH` points to the destination registry JSON:

```json
{
  "default-archive": {
    "kind": "archive",
    "adapter": "xarta-http-archive",
    "current_revision": "v1",
    "revisions": {
      "v1": {
        "endpoint": "http://archive:8000/api/v1/archive",
        "timeout": 30,
        "concurrency": 10,
        "archive": "default"
      }
    }
  }
}
```

The endpoint is the archive API base, without `/documents`. Every destination
requires explicit revisions; all retained revisions are validated at startup.
`timeout` is a positive number of seconds and `concurrency` bounds concurrent
document groups. When `default-archive` exists, it is the default destination for
archive nodes. Otherwise select a destination explicitly.

Without a destination configuration file, the service builds `default-archive`
from `ARCHIVE_SERVICE_ENDPOINT` and `ARCHIVE_SERVICE_TIMEOUT` (default 30 seconds),
with concurrency 10. The synchronous worker resolves the current destination on
execution rather than persisting a tracked adapter binding across retries; keep
destination routing stable while retrying writes with the same identities.

### Logical archives and storage backends

`ARCHIVE_STORAGE_CONFIG_PATH` points to a separate registry. This filesystem
example uses no credentials:

```json
{
  "backends": {
    "primary": {
      "current_revision": "v1",
      "revisions": {
        "v1": {
          "kind": "filesystem",
          "root": "/mnt/archive"
        }
      }
    }
  },
  "archives": {
    "default": {
      "backend": "primary",
      "policy": {
        "write": "versioned",
        "history": "retain-all",
        "require_current_predecessor": true,
        "duplicate_unchanged": true
      }
    }
  }
}
```

Filesystem roots must be persistent and accessible to every replica that reads or
cleans up those objects. New filesystem objects use the hardcoded sharded layout
described below; S3 objects use the logical archive/document/version hierarchy.

For S3, a backend revision uses `kind: "s3"` and requires `bucket`. Optional fields
are `prefix`, `endpoint_url`, `region_name`, `aws_access_key_id`,
`aws_secret_access_key`, `aws_session_token`, `use_ssl`, and `verify`. Omitted
credentials can use the SDK's normal credential resolution. Configure credentials
through deployment secrets, and use an S3-compatible service supporting the
conditional object writes used by archive.

New writes use the backend's `current_revision`. Existing representations retain
their original backend name and revision. Keep old revisions configured and their
locations reachable while referenced; changing `current_revision` does not migrate
existing objects. Do not repoint an existing revision to a different location.

Without a storage configuration file, the fallback is archive `default`, using
filesystem storage rooted at `ARCHIVE_STORAGE` (code default `archive`). Policy
defaults are `versioned`, `retain-all`, `require_current_predecessor: false`, and
`duplicate_unchanged: true`. Local development intentionally overrides some of
these defaults; see [Archive development](development/archive.md).

### Hardcoded storage-key policies

Key layout belongs to the backend implementation, not to an archive-specific
configuration option. All named archives on a backend use that backend's policy:

| Backend | Relative key for a new representation |
|---|---|
| Filesystem | `{archive}/{shard_high}/{shard_mid}/{shard_low}/{document_id}-{version_id}-{representation_id}.{allocation_id}` |
| S3-compatible | `{archive}/{document_id}/{version_id}/{representation_id}.{allocation_id}` |

The three filesystem shard levels are exactly two lowercase hexadecimal characters each:

```python
shard = zlib.crc32(document_id.bytes) & 0xFFFFFF
shard_high = f"{shard >> 16:02x}"
shard_mid = f"{(shard >> 8) & 0xFF:02x}"
shard_low = f"{shard & 0xFF:02x}"
```

`UUID.bytes` is the canonical 16-byte, network-order UUID representation. CRC32
uses the standard-library `zlib.crc32` default initial value, zero. The low 24 bits
select a directory; they do **not** replace any document, version, or representation
identity. Filenames contain all three full logical UUIDs plus a private allocation
suffix, with no media-type extension. `allocation_id` is a fresh UUIDv4 rendered as
32 lowercase hexadecimal characters, generated once per parsed snapshot. Sharing
that allocation suffix across its representations is safe because their logical
representation IDs must be unique. Independent attempts never deliberately reuse
an allocation suffix.

For example, document `c3be90e0-24ab-5d38-a39d-3149db97ae22` maps to `66/6a/05`:

```text
invoices/66/6a/05/c3be90e0-24ab-5d38-a39d-3149db97ae22-6518eb3c-c1eb-5f49-a1bf-67c91308e31e-a5f2a8ae-6f5d-59f8-bcff-51e359176286.781c6d4940f34d28bda2d994d12746ab
```

The suffix is a sibling filename, not a subdirectory below the previous key. An
existing unsuffixed key may already be a regular file. The three CRC32 shard levels
are unaffected by allocation identity, so retries stay in the same leaf while
using different filenames.

#### Why hash the document ID?

Putting every document directly below its archive creates a high-fan-out directory
and a separate directory for each document and version. In the new layout, three
shard levels cap each upper directory at 256 child directories and provide
16,777,216 possible leaf directories per archive, created lazily. Representation
files go directly in those leaves, avoiding per-document and per-version directories.
The 24-bit policy targets the expected 200-million-document scale: with one retained
file per document, uniform distribution means about 12 files per possible leaf.
Sparse archives can have nearly one occupied leaf per document; the larger shard
space trades additional directory metadata for smaller leaves at high volume.
Sharding changes the directory layout, not the number of committed
representation files. Count retained history, multiple representations, pending
writes, and orphan allocations when estimating leaf occupancy:

| Physical files per archive | Average per leaf at uniform distribution |
|---|---:|
| 100 million | 6.0 |
| 200 million | 11.9 |
| 1 billion | 59.6 |

Using the first three UUID bytes directly assumes well-distributed prefixes. That
works for UUIDv4/v5, but Xarta also accepts caller-supplied UUIDs. UUIDv7 starts
with a timestamp; documents created around the same time share their leading
bytes. CRC32 mixes all 16 bytes cheaply instead of relying on a UUID version or
prefix distribution. A reproducible experiment with one million UUIDv7-shaped IDs
sharing a timestamp and incrementing their low tail put every raw-prefix key in
one bucket. The distribution-check command below measures occupied CRC32 buckets
for that same workload; averages in the table above use all possible buckets.

CRC32 is a fast, non-cryptographic **bucketing** function. Bucket collisions are
expected and do not cause filename collisions: full identities remain in the
filename. The existing SHA-512 checksums still verify representation bytes. Do not
use CRC32 for content integrity, authentication, or adversarial collision resistance.
Python's built-in `hash()` is not the layout contract: its results are not promised
stable across runtimes/architectures, and string hashes are process-salted.

All versions and representations of one document share a leaf. This improves
locality but means exceptionally large per-document histories can still make a
leaf large. At that ceiling, a future policy can shard by version/representation;
the persisted-key design below allows new policies without relocating existing
objects. Distribution is a performance expectation, not a hard per-leaf size bound.

S3 prefixes are object-key namespaces, not filesystem directories. This filesystem
fan-out problem does not justify hashing S3 keys, so S3 performs no shard hashing.

#### Compute once, persist, and reuse

For **both** backends, `archive_document_representations` stores the completed
`storage_key`, `backend`, and `backend_revision`. The database key is relative to
the configured location:

- Filesystem: `{root}/{storage_key}`.
- S3: bucket `{bucket}`, object key `{prefix}/{storage_key}` (or just `storage_key`
  when no prefix is configured).

Root, bucket, and prefix are resolved through the pinned backend revision; they
are not copied into every row. Never repoint an existing revision to a different
location while objects still reference it. Completed content deletion clears
`storage_key` when the representation becomes metadata-only.

The write parser requests one backend key prefix **per submitted snapshot**, outside
the representation loop. Filesystem sharding therefore hashes only 16 bytes once,
regardless of representation count or file sizes. It generates one private
allocation UUID, appends each representation UUID and that allocation suffix, and
persists each complete key. It does not hash uploaded content for sharding,
maintain a global UUID cache, or add a database lookup for shard selection.

Reads, conditional writes, retries inside storage, rollback, and deletion consume
the supplied/stored key verbatim; they do not regenerate it or recalculate a shard.
Existing recorded hierarchical and two-level-sharded filesystem keys resolve through the
same root-and-key lookup. New writes use the sharded policy without a data migration
or special old-key branch. Changing a policy does not move existing objects;
an existing high-fan-out directory tree would require a separate relocation or
cleanup operation to compact it. New writes no longer add per-document directories.
Unreferenced orphan files from an interrupted older write remain an operational
cleanup concern, as before.

#### Measured CPU cost

Illustrative local measurements on Python 3.13.7, Darwin arm64: median of seven
repeats of 300,000 operations, after imports. These measure CPU-only key work,
not filesystem, database, network, or complete-request throughput.

| Operation | Time per operation |
|---|---:|
| CRC32 of an existing 16-byte value | ~50 ns |
| SHA-256 of an existing 16-byte value | ~290 ns |
| BLAKE2s of an existing 16-byte value | ~257 ns |
| UUID byte extraction + CRC32 + 24-bit selection | ~133 ns |
| Implemented three-level filesystem prefix, including UUID/shard formatting | ~1.67 µs |
| Implemented S3 prefix, including UUID formatting | ~1.10 µs |
| Fresh private allocation UUIDv4 in hex, once per snapshot | ~1.94 µs |

Prefix timings exclude the private allocation UUID and per-representation suffix
concatenation. The safety nonce is generated once per snapshot, not once per file.
Formatting dominates complete-prefix cost; the faster hash is not a proportional
speedup of the whole write path. There is no sharding-hash or allocation-UUID cost
on reads or deletion.
Reproduce the relevant operations on the deployment's own Python/CPU with:

```sh
uv run --frozen python - <<'PY'
from statistics import median
from timeit import repeat
from uuid import UUID, uuid4
from zlib import crc32
from xarta.services.v1.archive.storage import FilesystemStorageBackend, S3StorageBackend

document = UUID("c3be90e0-24ab-5d38-a39d-3149db97ae22")
version = UUID("6518eb3c-c1eb-5f49-a1bf-67c91308e31e")
for statement in (
    "crc32(document.bytes) & 0xffffff",
    "uuid4().hex",
    "FilesystemStorageBackend.representation_key_prefix('invoices', document, version)",
    "S3StorageBackend.representation_key_prefix('invoices', document, version)",
):
    timings = repeat(statement, globals=globals(), number=300_000, repeat=7)
    print(statement, median(timings) / 300_000 * 1e9, "ns/op")
PY
```

The fixed-prefix distribution experiment is also independent of storage I/O:

```python
from collections import Counter
from uuid import UUID
from zlib import crc32

base = int("0199aabbccdd70008000000000000000", 16)
buckets = Counter(crc32(UUID(int=base + n).bytes) & 0xFFFFFF for n in range(1_000_000))
print(len(buckets), min(buckets.values()), max(buckets.values()))
```

### Database and dependencies

Archive relational state lives in its own PostgreSQL database, migrated by the
Sqitch project under [`db/archive`](../db/archive). It uses
`ARCHIVE_POSTGRESQL_HOST`, `ARCHIVE_POSTGRESQL_DATABASE`,
`ARCHIVE_POSTGRESQL_USER`, and `ARCHIVE_POSTGRESQL_PASSWORD`. The expiration job
uses the same archive database configuration; other services use their application
database settings.

The service also needs NATS JetStream, the configured storage backends, temporary
storage for temporary sources, document-type service/cache configuration, and any
services required by selected document sources. `make db-deploy` applies both local
database projects. For existing database cutovers, follow the
[database guide](../db/README.md).

Configuration is loaded at startup. Restart the service after changing destination
or storage registries.

### Concurrency and cleanup settings

| Environment variable | Default | Purpose |
|---|---|---|
| `ARCHIVE_REPRESENTATION_WRITE_CONCURRENCY` | `10` | Concurrent representation writes within a snapshot; range 1–100. |
| `ARCHIVE_DELETION_RECONCILER_ENABLED` | `true` | Enable cleanup consumption and recovery. |
| `ARCHIVE_DELETION_RECONCILER_BATCH_SIZE` | `100` | Maximum messages fetched per cleanup batch. |
| `ARCHIVE_DELETION_RECONCILER_CONCURRENCY` | `25` | Concurrent cleanup processing; range 1–100. |
| `ARCHIVE_DELETION_RECONCILER_SCAN_SECONDS` | `5` | Periodic desired-state recovery interval. |
| `ARCHIVE_DELETION_RECONCILER_ACK_WAIT_SECONDS` | `120` | Cleanup consumer acknowledgement wait. |
| `ARCHIVE_DELETION_RECONCILER_MAX_BACKOFF_SECONDS` | `3600` | Maximum cleanup retry delay. |

These limits are separate from outbound destination `concurrency` and NATS worker
concurrency. The implementation materializes source and upload bytes in memory,
and downloads read full content before integrity verification. Size process memory
and concurrency together; these paths are not streaming transfers.

The core Helm chart exposes archive deployment configuration through
`services.archive`, including `settings.adapters.config`, `existingSecret`,
`settings.storage.config`, `settings.storage.existingSecret`,
`settings.storage.mounts`, and `settings.deletionReconciler`. The adapter Secret
contains `archive.json`; the storage Secret contains `archive-storage.json`.
See the [archive deployment template](../k8s/helm/charts/core/templates/deployments/service-archive.yaml)
and [expiration CronJob template](../k8s/helm/charts/core/templates/cronjobs/expire-archived-documents.yaml).

## Operations and troubleshooting

| Symptom | What to check |
|---|---|
| `version_conflict` after a retry | Keep the same key, timestamps, IDs, metadata, and bytes. Check whether a source was re-rendered or resolved a different `HEAD`. |
| `version_conflict` on an update | Read current `HEAD`; check `parent_version_id`, predecessor policy, and reuse of a requested version identity. |
| `unchanged` despite a new version ID | Equivalent-snapshot detection is enabled. Use the returned existing version identity. |
| `overwrite_rejected` | The receiving archive uses `create-only`. |
| Unknown archive | Check the logical archive registry on the receiving service, not just the worker's destination names. |
| `300` on a download | Several representations match equally. Select an exact representation. |
| `406` on a download | The requested `Accept` does not match an available representation. |
| Details exist but historical content is unavailable | Check representation state and history policy; `metadata-only` content was intentionally removed. |
| Content unavailable unexpectedly | Check aggregate lifecycle, backend revision availability, and the stored object's existence. |
| Integrity verification failure | Investigate stored-object corruption or external modification; content is deliberately not returned. |
| Deletion stays pending | Check the reconciler is enabled, JetStream commands, storage access, retained backend revisions, and cleanup error logs. |
| Content still exists after expiration | Check the expiration job schedule and batch size, current `HEAD` expiration, and cleanup consumer progress. |
| Workflow failure with some documents present | Multi-document submission is not atomic; retry stable requests and inspect each document's accepted state. |

The focused regression suite is:

```console
make archive-test
```

It covers workflow submission, version policies, immutable storage, replay,
rollback, deletion, expiration, reconciliation, and event publication. The local
end-to-end generation/signing/archive scenario is:

```console
make scenario-generate-sign-archive
```

Scenarios manage their own service processes; follow
[CONTRIBUTING.md](../CONTRIBUTING.md) and the local
[archive development guide](development/archive.md) for environment setup.

## Implementation map

| Area | Source |
|---|---|
| Workflow snapshot parser and outcomes | [`protocol/dag/archive/base.py`](../src/xarta/protocol/dag/archive/base.py) |
| Worker, stable identities, and outcome emissions | [`services/v1/archive/nats.py`](../src/xarta/services/v1/archive/nats.py) |
| HTTP adapter, grouping, and retry classification | [`services/v1/archive/adapters.py`](../src/xarta/services/v1/archive/adapters.py) |
| Multipart parsing and fingerprints | [`services/v1/archive/store.py`](../src/xarta/services/v1/archive/store.py) |
| Transactions, replay, lifecycle, and queries | [`services/v1/archive/db.py`](../src/xarta/services/v1/archive/db.py) |
| Storage registries and backend implementations | [`services/v1/archive/storage.py`](../src/xarta/services/v1/archive/storage.py) |
| Downloads and negotiation | [`services/v1/archive/download.py`](../src/xarta/services/v1/archive/download.py) |
| Details and pagination | [`services/v1/archive/details.py`](../src/xarta/services/v1/archive/details.py) |
| Deletion API and consumer | [`delete.py`](../src/xarta/services/v1/archive/delete.py), [`reconciler.py`](../src/xarta/services/v1/archive/reconciler.py) |
| Event envelope and publication | [`services/v1/archive/events.py`](../src/xarta/services/v1/archive/events.py) |
| Archive document-source resolver | [`protocol/document/source.py`](../src/xarta/protocol/document/source.py) |
| Expiration job | [`jobs/expire_archived_documents/base.py`](../src/xarta/jobs/expire_archived_documents/base.py) |
