# Authoritative archive metadata search

The archive service searches its own PostgreSQL metadata. Every active archived
document is eligible without a `search-index` node, external search engine, or
access to its stored bytes. Filesystem/S3 placement does not affect matching.

- [Practical examples and client loop](#what-this-means-in-practice)
- [Filter rules](#request-and-matching-rules)
- [Response and version-pinned retrieval](#response)
- [Cursor walkthrough](#cursor-continuation)
- [Cursor internals](#how-the-cursor-works-internally)
- [Errors](#errors-and-execution-budget) and [indexing](#indexing-and-upgrade)

```text
POST /api/v1/archive/archives/{archive}/documents/search
POST /api/v1/archive/documents/search
```

The second route selects `default`. Floating `/api/archive/...` aliases are also
available. POST carries a typed JSON query; the operation is read-only and creates
neither a workflow nor a stored query session.

## What this means in practice

Think of search as asking: **"Which current documents have these metadata values?"**
For example, an accounting process can find customer `CUST-0042`'s invoices for
2026, inspect their metadata, and download the exact versions that matched.

Search looks at the logical metadata attached to each document's current HEAD.
If an old version said `customer_id: CUST-0042` but HEAD says `CUST-0099`, a search
for `CUST-0042` does not match that document. A matching value found only in a PDF
representation's metadata also does not count.

You do not need to run an indexing workflow first. The query reads the archive
database directly. It does not open PDFs, scan S3 buckets, inspect filesystem
shards, or search document text.

### First request with curl

```sh
export XARTA_URL=http://localhost:8000

curl --fail-with-body "$XARTA_URL/api/v1/archive/documents/search" \
  --header 'Content-Type: application/json' \
  --data '{
    "filter": {
      "document_type": "invoice",
      "metadata": {"customer_id": "CUST-0042", "fiscal_year": 2026}
    },
    "limit": 50
  }'
```

Read `items` for the matches and `pagination.next` to know whether to continue.
An empty `items` array with `next: null` means the query completed without further
matches. A failure is a non-success HTTP response, not an empty successful result.

### A complete client loop

This example uses Xarta's existing `aiohttp` dependency and reuses one HTTP session.
Run it in the repository environment with `uv run python your_script.py`. Set
`XARTA_URL` to the origin of the service or gateway. Supply any authentication
headers required by that deployment through the client session.

```python
import asyncio
import os

import aiohttp


async def main():
    origin = os.environ.get("XARTA_URL", "http://localhost:8000").rstrip("/")
    endpoint = f"{origin}/api/v1/archive/documents/search"
    payload = {
        "filter": {
            "document_type": "invoice",
            "metadata": {"customer_id": "CUST-0042", "fiscal_year": 2026},
        },
        "limit": 50,
    }
    timeout = aiohttp.ClientTimeout(total=65)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            async with session.post(endpoint, json=payload) as response:
                response.raise_for_status()
                batch = await response.json()

            for document in batch["items"]:
                print(document["id"], document["version"]["id"], document["metadata"])

            next_cursor = batch["pagination"]["next"]
            if next_cursor is None:
                break
            payload["cursor"] = next_cursor


asyncio.run(main())
```

The cursor goes in the **JSON body** for search, not in the URL. Repeat the filter;
the cursor does not supply it for you. Always inspect `next` rather than deciding
that a full batch must have a successor or that an empty response means an error.

For real processing, advance your saved checkpoint only after successfully handling
the whole batch. Make side effects idempotent using the matched document/version
identity: a retry reads current state and is not guaranteed to reproduce an identical
batch. This loop is a traversal example, not an exactly-once work queue.

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
- Arrays, NUL characters, nonfinite numbers, and unknown request/filter fields are
  rejected. There is no range/OR/wildcard operator syntax; scalar strings are always
  literal values. Stored metadata may contain arrays; the full metadata is returned,
  but arrays are not search predicates in this API.
- The request must be `application/json` and at most 16,384 UTF-8 body bytes.
- Search parameters must be in that JSON body. Query-string parameters are rejected
  rather than silently ignoring a misplaced cursor and restarting traversal.
- `limit` is an integer from 1 through 100, default 50. JSON booleans, strings,
  fractional values, and null are invalid limits.

Some common distinctions:

| Filter | Interpretation |
|---|---|
| `{"metadata":{"fiscal_year":2026}}` | Match the number 2026, not the string `"2026"`. |
| `{"metadata":{"approved":false}}` | Match an explicit boolean false; a missing field does not match. |
| `{"metadata":{"external_reference":null}}` | Match an explicitly stored null; a missing key does not match. |
| `{"metadata":{"customer":{"country":"BE"}}}` | Match the nested field. Other fields inside `customer` may exist. |
| `{"metadata":{"customer.country":"BE"}}` | Match a literal key named `customer.country`, not the nested field. |
| `{"document_type":null}` | Select HEADs with no document type. |
| `{}` | No filter restrictions; enumerate active HEADs in bounded batches. |

Neither `*` nor a string such as `"2026-01-01..2026-12-31"` invokes an operator:
strings are literal values. The API does not interpret an SQL, regex, JSONPath, or
range-expression language inside metadata values.

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

### Worked example: why the extra row exists

Suppose the matching logical document IDs, in order, are **A, B, C, D**. These
letters are explanatory labels for UUIDs. With `limit: 2`:

| Request | Database result (at most 3 rows) | Returned `items` | `pagination.next` |
|---|---|---|---|
| First, without a cursor | A, B, C | A, B | Bookmark for B |
| Continue after B | C, D | C, D | null |

The server fetches one extra matching row to determine whether continuation exists.
**The bookmark points to B, the last returned row, not C, the lookahead row.**
Otherwise the next request would skip C. C is not reserved or cached: the next
request queries the database again.

The final response still reports `pagination.limit: 2`, even though there is no
continuation. With a requested limit of 50 and only two matches, it would report
`limit: 50`, two items, and `next: null`.

## How the cursor works internally

**The cursor is not a Redis/cache lookup key.** There is no saved result set or
pagination session behind it. The token itself carries enough information to
continue: a label identifying the query and the last returned document ID.

| Piece | Purpose |
|---|---|
| Query hash inside `scope` | Compare the new request's criteria with those used to create the bookmark. |
| `values.document_id` | Supply the position for the next `document_id > ...` seek. |
| Encoded cursor | Carry both pieces back to the server in one token. |

For example, a cursor issued for customer `CUST-0042` should not accidentally be
used to continue a query for `CUST-0099`. The different filters produce different
query hashes, so that reuse is rejected. The hash does not locate cached matches,
retrieve a stored filter, or identify a physical document object.

The client must repeat its filter because the hash does not contain the full query.
PostgreSQL executes a fresh query for every batch; it is not reading the next slice
of a cached result. This is also why changes between requests can affect results.

This section explains the implementation for maintainers. The token format is not
a client contract; clients should copy the token unchanged rather than build it.

### 1. Identify the query for comparison—not cache lookup

`search_cursor_scope()` computes a SHA-256 fingerprint of a small JSON object:

```json
{
  "archive": "default",
  "filter": {
    "document_type": "invoice",
    "metadata": {"customer_id": "CUST-0042", "fiscal_year": 2026}
  },
  "order": "document_id:asc",
  "versions": "head"
}
```

It uses normalized filters and sorted object keys. The digest is calculated once
per request, not once per document. It fingerprints the query, not the stored
document metadata or file bytes. It is unrelated to the CRC32 storage shard hash.
`limit` is deliberately excluded so that clients may change batch size.

### 2. Package the last returned position

The generic encoder in `src/xarta/http/pagination.py` serializes an envelope like
this, then encodes it as URL-safe base64:

```json
{
  "version": 1,
  "scope": "archive-head-metadata:v1:8f05bd74a8e3e41243a248fc7d48c351a1fce12732f27ded5cadc33c4fc59e01",
  "values": {
    "document_id": "20000000-0000-4000-8000-000000000002"
  }
}
```

This example uses the filter above and an illustrative last returned document ID.
`version: 1` is the cursor-envelope version, not a document's version. The position
is a **logical document UUID**, not a version UUID, physical storage key, shard ID,
or count of preceding records.

Base64 is neither encryption nor a signature. The digest binds the query identity
but is not a secret or MAC. A client with implementation knowledge could construct
a token. Correctness/access control must therefore come from the archive, filter,
lifecycle, and deployment access predicates—not from trusting the cursor.

### 3. Validate a continuation

For the next request the server recomputes the expected scope from the submitted
archive and filter. The decoder checks the token length, base64, JSON envelope,
supported encoding version, exact scope, expected position fields and value types.
The search handler then parses `document_id` as a UUID. A malformed token or a token
from a different query produces HTTP 400 instead of silently changing the query.

The current token has no time-based expiry and does not refer to a stored query
session. An older bookmark resumes against current data, not a retained snapshot.
Future encoding/scope revisions may invalidate older tokens.

Other collections reuse the same envelope codec but have their own scopes and
position fields. An archive version-history cursor, for example, is not a search
cursor and cannot be substituted for one.

### 4. Seek after the bookmark

The next query retains all original predicates and adds:

```sql
AND aggregate.document_id > $cursor_document_id
ORDER BY aggregate.document_id ASC
LIMIT $requested_limit_plus_one
```

The parameter names here are explanatory; the implementation uses bound PostgreSQL
parameters. There is no `OFFSET` and no open database cursor held between HTTP
requests. PostgreSQL can use indexes to seek past the bookmark instead of counting
and discarding all preceding results. The GIN index assists metadata matching;
filter selectivity and ordering can still require processing more rows than the
number returned.

The bookmarked document does not have to still exist. Its UUID is a comparison
boundary; the server does not need to look up that document to resume traversal.

### What happens while documents change?

- A newer HEAD does not change the logical document ID or its ordering position.
- A document after the bookmark can enter or leave the matching set before the
  next request. The next request uses its then-current HEAD.
- A newly matching document at or before the bookmark will not be visited during
  the remainder of that traversal.
- A version returned in an earlier batch remains the identity of the snapshot
  that matched then; use its version-pinned URL for follow-up retrieval.

This makes the mechanism suitable for bounded discovery and enumeration. Repeated
polling from an old terminal cursor is not a reliable way to detect every update
to existing documents. A change feed or a separate snapshot/export design would
provide different guarantees.

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
