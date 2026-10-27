# Archive Lifecycle Events

Archive publishes lifecycle facts directly to JetStream, independently from DAG outcomes and
search indexing. PostgreSQL has no archive event outbox. Event IDs are deterministic from the
event type and internal aggregate/version generation and are also supplied as `Nats-Msg-Id`.

## Delivery

The `DOCUMENTS` stream stores subjects shaped as:

```text
documents.archive.<archive-token>.<document-uuid>.<event-token>
```

`archive-token` is the archive name encoded as unpadded base64url. Event tokens are
`created`, `version-created`, `deletion-requested`, and `deleted`.

Accepted JetStream events can be delivered more than once, subject to stream retention
and consumer configuration. Creation and version events publish after database commit,
and an exact write replay republishes the same deterministic fact. Persisted document
deletion intent is scanned at startup and periodically to recover commands lost
after commit.

Deletion requests schedule work; only deletion-command completion finalizes a
document. Supported writes always create at least one available representation.
Partial deletion leaves available work, and the transaction completing the last
representation also commits the tombstone under the aggregate lock. Consequently,
a request sees either available work or an already-deleted document, never a
committed intermediate state with no work and no tombstone. If manually altered or
inconsistent database state violates this invariant, the request fails and its
transaction rolls back instead of finalizing without a recoverable command.

Final document deletion is recoverable through deletion-command redelivery. When a
command finds an already-completed operation, it checks for a deleted tombstone matching
the internal aggregate ID, archive, and public document ID. A matching tombstone causes
`xarta.archive.document.deleted` to be published again before the command is acknowledged.
An active/deleting document or a mismatched identity does not establish this fact.

The first deleted-event publication and every retry use the tombstone's persisted deleted
HEAD identities and `updated_at` timestamp. Finalization captures the authoritative HEAD
under the aggregate lock. Event
ID, occurrence time, and payload are therefore stable across retries. The database
connection is released before awaiting PubAck. Publication failure leaves the command
eligible for redelivery; an ambiguous PubAck or command ACK can cause duplicate publication.
Consumers must tolerate duplicates outside the ten-minute JetStream deduplication window.

Recovery depends on the deletion command remaining in the work queue and an enabled
deletion consumer eventually processing it. There is no independent tombstone event scan.
Deletion-requested publication still has a post-commit loss window. Repeating an HTTP DELETE
against a tombstone returns 200 without publication. Final-state convergence does not
promise delivery of every superseded historical transition or exactly-once delivery.

`deletion-requested` is advisory: it provides early notice that the document is deleting,
but consumers must not require it before processing `deleted`. The final `deleted` fact
supersedes that notice. The search consumer may remove projections early on
`deletion-requested` and also handles `deleted` independently. This is a notification
contract, not fire-and-forget publication: the request handler still awaits publication,
and a publication error can fail the HTTP request after its database transition committed.
Writers must therefore retry with the same `Idempotency-Key`; see
[Always send an Idempotency-Key](archive-capability.md#always-send-an-idempotency-key).

## Envelope

Creation events contain a complete immutable snapshot:

```json
{
  "specversion": "1.0",
  "id": "ed3219a8-296e-4eb0-a084-d3fd74fa3d6e",
  "type": "xarta.archive.document.version-created",
  "source": "urn:xarta:archive",
  "subject": "payroll/2f91a80d-06d6-44e4-a0aa-ce67a89dcdd3",
  "time": "2026-08-28T12:34:56.123456Z",
  "datacontenttype": "application/json",
  "data": {
    "archive": "payroll",
    "id": "2f91a80d-06d6-44e4-a0aa-ce67a89dcdd3",
    "version": {
      "id": "4f93c246-1ada-4789-87c0-4a262ce30e54",
      "parent": "af24242e-ee22-4dc8-9594-f1f8052bb0e0"
    },
    "document_type": "payslip",
    "metadata": {"employee": "E-0042", "period": "2026-08"},
    "representations": {
      "default": "9b167514-f403-4771-8db9-9476a9a2d6bd",
      "items": [{
        "id": "9b167514-f403-4771-8db9-9476a9a2d6bd",
        "name": "payslip-2026-08.pdf",
        "metadata": {"role": "human-readable"},
        "content": {
          "type": "application/pdf",
          "checksum": {"sha512": "<128 lowercase hexadecimal characters>"},
          "size_bytes": 48213,
          "availability": "available"
        }
      }]
    },
    "outcome": "version_created"
  }
}
```

`data` uses the same resource shapes as the HTTP API: `id` is the logical document
UUID, `version` is the `{id, parent}` identity of the new HEAD, and
`representations` matches the details response, including server-computed
`content.checksum` and `content.size_bytes`. `outcome` is `created` or
`version_created`.

Deletion events carry no snapshot. `head` is the UUID of the document's HEAD
version when deletion was requested or finalized:

```json
{
  "specversion": "1.0",
  "id": "5d0f9f8e-3b8a-5f0e-9c55-0a6f3c1e2b77",
  "type": "xarta.archive.document.deleted",
  "source": "urn:xarta:archive",
  "subject": "payroll/2f91a80d-06d6-44e4-a0aa-ce67a89dcdd3",
  "time": "2036-09-01T00:15:03.402118Z",
  "datacontenttype": "application/json",
  "data": {
    "archive": "payroll",
    "id": "2f91a80d-06d6-44e4-a0aa-ce67a89dcdd3",
    "head": "4f93c246-1ada-4789-87c0-4a262ce30e54",
    "outcome": "deleted"
  }
}
```

`deletion-requested` events have the same shape with `outcome`
`deletion_requested`.

Payload fields are allow-listed. Events never expose storage keys, backend names or
revisions, credentials, buckets, paths, or content. Version and representation metadata may
contain sensitive application data, so `DOCUMENTS` access and retention must be appropriate.

## Event Types

| Type | State change |
|---|---|
| `xarta.archive.document.created` | First immutable snapshot becomes `HEAD` |
| `xarta.archive.document.version-created` | A subsequent immutable snapshot becomes `HEAD` |
| `xarta.archive.document.deletion-requested` | Advisory early notice that the aggregate changes from `active` to `deleting`; not a prerequisite for `deleted` |
| `xarta.archive.document.deleted` | All representation content is gone and the aggregate is finalized |

Consumers must use `archive`, `id`, `version.id`/`head`, and representation identities rather than
assume strict ordering. JetStream sequence reflects broker acceptance order only.
