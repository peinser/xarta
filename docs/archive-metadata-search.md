# Authoritative archive metadata search

The archive service searches its own PostgreSQL metadata. Every active archived
document is eligible without a `search-index` node, external search engine, or
access to its stored bytes. Filesystem/S3 placement does not affect matching.

```text
POST /api/v1/archive/archives/{archive}/documents/search
POST /api/v1/archive/documents/search
```

The second route selects `default`. Floating `/api/archive/...` aliases are also
available. POST carries a typed JSON query; the operation is read-only and creates
neither a workflow nor a stored query session.

## Request and matching rules

```json
{
  "filter": {
    "document_type": "invoice",
    "metadata": {
      "customer_id": "CUST-0042",
      "fiscal_year": 2026
    }
  },
  "limit": 50
}
```

- `filter` is optional. Omitting it, or supplying `{}`, traverses all active HEADs
  in the selected archive in bounded batches.
- `document_type` is an exact match. Omit it for any type; explicit null selects
  documents whose HEAD has no type.
- `metadata` is an object of AND-combined scalar matches. Additional stored fields
  are allowed. Strings are case-sensitive; numbers, strings and booleans are not
  coerced into each other. Null matches an explicitly stored null, not a missing key.
- Nested objects express nested matching. Dots in a key are literal, not JSONPath.
  An empty root metadata filter is equivalent to omission; empty nested objects
  are rejected. Nested objects may extend at most eight levels below the root.
- Arrays, range/OR/wildcard expressions, NUL characters, nonfinite numbers, and
  unknown request/filter fields are rejected. Stored metadata may contain arrays;
  the full metadata is returned, but arrays are not search predicates in this API.
- The request must be `application/json` and at most 16,384 UTF-8 body bytes.
- Search parameters must be in that JSON body. Query-string parameters are rejected
  rather than silently ignoring a misplaced cursor and restarting traversal.
- `limit` is an integer from 1 through 100, default 50. JSON booleans, strings,
  fractional values, and null are invalid limits.

Only the current HEAD's **logical document metadata** participates in matching.
Historical versions and representation metadata are not searched. A historical
match never causes a different current version to be returned as a match.
Deleting/deleted documents are excluded. Expiry alone is not an access cutoff:
an expired but active document can still match, just as it can still be read.

A well-formed archive namespace with no active matches returns an empty HTTP 200
collection, including a namespace with no documents. The query does not depend on
the current storage registry. Archive names follow the shared archive-name validator:
an ASCII letter/digit first, then ASCII letters/digits, dots, underscores or hyphens.

## Response

```json
{
  "archive": "default",
  "items": [
    {
      "id": "c3be90e0-24ab-5d38-a39d-3149db97ae22",
      "version": {
        "id": "6518eb3c-c1eb-5f49-a1bf-67c91308e31e",
        "parent": null
      },
      "document_type": "invoice",
      "metadata": {
        "invoice_number": "INV-2026-0042",
        "customer_id": "CUST-0042",
        "fiscal_year": 2026
      },
      "created_at": "2026-10-14T09:30:00+00:00",
      "expires_at": "2036-10-14T09:30:00+00:00"
    }
  ],
  "pagination": {"limit": 50, "next": null}
}
```

These are lightweight metadata results. The enclosing `archive` scopes every
item; representations are not aggregated or downloaded during search. Each result
pins the immutable version that matched. For subsequent access use:

```text
GET /api/v1/archive/archives/{archive}/documents/{id}/versions/{version.id}/details
GET /api/v1/archive/archives/{archive}/documents/{id}/versions/{version.id}
```

Using the unversioned URL instead could retrieve a newer HEAD than the one that
matched. Logical metadata remains immutable; an update creates a new snapshot.

## Cursor continuation

Ordering is logical document UUID ascending. A HEAD update therefore does not
move the document to a different ordering position.

1. Omit `cursor` on the first request.
2. Repeat the same filter and supply the returned `pagination.next` in the next
   request's JSON `cursor` field. A non-null token is opaque, not a URL/page number.
3. Stop when `pagination.next` is null. Do not send null as a cursor.

`pagination.limit` is the effective maximum batch size even on a short or empty
batch. It may change between requests. There are no offsets, numbered pages,
total counts or total-page calculations.

Cursors bind the archive, normalized filter, HEAD scope, and ordering. Empty root
metadata filters are normalized away and object key ordering is immaterial; repeat
other filter values as submitted. Tokens from another collection/filter are
rejected. Cursor encoding is versioned, bounded to 2,048 characters, and checked
for valid base64, envelope structure, value types and UUID position. Cursors are
continuation positions, not signed authorization credentials; server-side archive,
filter and lifecycle predicates are applied independently on every request.

Each query observes committed database state for that request. This is not a
frozen multi-request snapshot: concurrent writes can change membership between
batches. A new matching document before the current cursor position will not be
visited during that traversal. Use returned version identities for reproducible
follow-up work; this endpoint is not a change feed.

## Errors and execution budget

Errors follow the archive `error: {code, message}` envelope:

- 400: invalid request/filter/name/limit, malformed or mismatched cursor.
- 413: request body exceeds 16 KiB.
- 415: unsupported media type.
- 503: database timeout, cancelled query or connection failure. Failure is never
  reported as an empty successful search.

Search uses the existing archive pool command timeout
(`ARCHIVE_POSTGRESQL_COMMAND_TIMEOUT`, default 60 seconds). Deployment-level request
and ingress timeouts still apply. The usual deployment access controls cover this
endpoint; an archive name is not an authorization boundary.

## Indexing and upgrade

Deploy `00001.archive-metadata-search` in the independent `db/archive` Sqitch project
before enabling search traffic. See [database upgrades](../db/README.md).

The new GIN index uses `jsonb_path_ops` on version metadata for JSONB containment
(`@>`). It covers existing and future versions automatically. The query joins only
each active document's current HEAD and uses indexed identity comparisons for
cursor seek. Existing archive/document identity, HEAD, version-primary-key and
document-type indexes remain useful. No representation table or object backend
participates in search.

The query returns at most `limit + 1` result rows and uses the extra row only to
determine continuation. This does not bound all database work: GIN can produce
many candidates, and it does not supply document-ID ordering. Performance at
200-million-document scale must be measured with representative data and
`EXPLAIN (ANALYZE, BUFFERS)`, not inferred from the small result limit.

Optimization should follow the measured plan:

- If specific metadata filters dominate, consider targeted indexes together with
  matching typed predicates; adding an expression index alone does not rewrite a
  generic `@>` query.
- If inactive document rows dominate traversal, an active-only archive/document
  seek index may justify its storage and maintenance cost.
- If historical candidates/index size dominate, a HEAD-only metadata projection
  maintained in the same transaction as HEAD changes can remain authoritative.
  An asynchronous search projection would have different consistency semantics.
- Duplicating archive names on version rows might reduce candidates in some
  multi-archive workloads, but does not remove the HEAD/lifecycle/identity join.

The initial implementation keeps the normalized data model and lets PostgreSQL
choose between eligible index paths. Integration tests prove index eligibility
and correctness on PostgreSQL; they are not a production-scale latency benchmark.
