# HTTP API v1 contract revisions

## 6.0.0 — 2026-10-17

- Remove automatic historical-content cleanup. Only the logical document can be
  deleted; all versions and representations remain retained until explicit deletion
  or HEAD-expiry-driven deletion of that document. Appending a new version never
  queues deletion of previous content.
- Breaking pre-release configuration change: remove `policy.history`. Deploy archive
  migration `00002.document-only-deletion` before starting the new archive service.
  Pending history-cleanup intent is cancelled. Previously removed bytes are not restored.
- Remove historical-content-removal lifecycle events and per-version search cleanup.
  Document deletion still removes every projection for the logical document.
  HTTP paths, response shapes, write identities, and tombstone behavior are unchanged.
- Clarify examples for retained predecessor versions and legacy metadata-only records.

## 5.1.0 — 2026-10-17

- Add read-only authoritative HEAD metadata search at the scoped and default
  archive `POST .../documents/search` endpoints. Accept exact typed scalar and
  nested-object filters, optional document type (including null), and bounded limits.
- Return lightweight logical metadata with the matching immutable version identity
  and the existing `pagination: {limit, next}` envelope. Continuation is submitted
  in the JSON `cursor` field, bound to archive, normalized filter, HEAD scope and
  ordering. No offsets or total counts; empty queries traverse active HEADs.
- Document body/depth bounds, supported predicates, expiry/lifecycle visibility,
  live cursor semantics, empty results, and 400/413/415/503 errors. Add representative
  request/response examples and the concurrent GIN-index deployment requirement.
- Harden the shared cursor decoder against invalid envelope types, unsupported
  versions and non-base64 input, and reject non-integer limits without truncation.
  Existing valid GET collection requests and server-issued cursors remain compatible.
## 5.0.1 — 2026-10-16

- Fix archive compensation ownership: independent submissions use private physical
  allocations, so delayed cleanup after a database rollback cannot remove content
  adopted and committed by a concurrent retry. Filesystem keys retain CRC32 shards.
- Document the failure boundary and remaining orphan-reclamation limitation.
  Logical IDs, semantic fingerprints, idempotency keys, HTTP response shapes, and
  status codes remain unchanged. A committed replay does not persist new objects.

## 5.0.0 — 2026-10-15

- Group cursor continuation as `pagination: {limit, next}` in archive version and
  document-type collections. Remove the top-level `next_cursor` response field.
- `limit` reports the effective maximum batch size, not the number of returned
  items. `next` is the opaque token to pass as the next request's `cursor`, or null
  when no further results exist. Document continuation and empty/final batches.
- Breaking pre-release response change. HTTP paths, query parameter names, cursor
  encoding/scope, indexed keyset traversal, and bounded fetching remain unchanged;
  no page numbers, offsets, or total-count queries are introduced.

## 4.0.0 — 2026-10-15

- Group archive reads under `representations: {default, items}`. Each item now uses
  `id` and `content: {type, size_bytes, checksum, availability}`; its metadata stays
  distinct from document-version metadata. Rename snapshot `state` to the explicit
  aggregate `content_availability` and document partial content retention.
- Return concise write acknowledgements: `archive`, `id`, `version`, and `outcome`.
  Representation IDs, metadata, checksums, and sizes are available through reads.
- Align HTTP manifests with nested `id`, `version: {id, parent}`, and representation
  groups. Upload items use `id`, `field`, `name`, `metadata`, and `content: {type}`.
  Unknown/obsolete fields and server-owned content properties are rejected.
- Rename HTTP snapshot timestamps to `created_at`/`expires_at` and specify timezone,
  caller-supplied creation time, and retention semantics. Validate identity and
  timestamp types at the archive HTTP boundary rather than silently treating false
  values as omitted.
- Standardize errors from matched archive routes as `error: {code, message}`;
  HTTP 300 includes `representation_ids`, never an array of objects under that name.
  Document retention-protected deletion as 403; missing/already deleted documents
  retain the existing empty 200 response.
- Breaking pre-release HTTP v1 revision. Callers must send and consume the new
  shapes. Routes, operation IDs, binary transport, workflow-node source schemas,
  stored archive data, and internal idempotency fingerprints remain unchanged.

## 3.0.0 — 2026-10-14

- Group archive response version identity under `version: {id, parent}` in details,
  write results, representation collections, and version-list items. `parent` is a
  UUID or `null`, never an embedded snapshot. Write responses include the actual
  stored parent, including unchanged results and idempotent retries.
- Standardize logical document identity as top-level `id` in those responses and
  the version-list envelope. The list retains its `head_version_id` pointer.
- Add the reusable `ArchiveVersionIdentity` schema and update all affected examples,
  including a subsequent version with a non-null parent.
- Breaking response-shape change applied directly to the unreleased HTTP v1 API.
  Update clients to read `version.id`/`version.parent` and `id`. Request manifests,
  workflow inputs, HTTP paths, and idempotency semantics remain unchanged.

## 2.0.0 — 2026-10-14

- Remove `replayed` from archive write responses and their schemas/examples.
  Idempotency remains unchanged: exact retries return the original outcome and
  identities with HTTP `200`, while conflicting reuse still returns `409`.
- Breaking response-field removal, applied directly to the unreleased HTTP v1
  contract under the pre-release policy. The contract revision major changes;
  the HTTP paths remain `/api/v1`.

## 1.0.1 — 2026-10-14

- Add named, realistic JSON/XML request and response examples throughout the HTTP
  contract, plus examples for every reusable schema and request parameter.
- Cover complete nonempty workflows, profile submission, archive representations,
  pagination, payment, callback duplicates, UBL validation, postal production and
  recovery, and structured/unstructured SEPA references. Use coherent synthetic
  identities and actual repository profile/UBL examples.
- Add file-oriented commands for multipart uploads, binary downloads, signing,
  rendering and QR generation; distinguish JSON form fields from JSON file parts.
- Preserve curated examples when refreshing executable schemas, and validate
  coverage, formats, and example schema conformance in `make contracts-check`/CI.
- Correct outcome `details` to allow the `null` value already emitted by serializers.
  Documentation correction only; no runtime API behavior changed.

## 1.0.0 — 2026-10-14

- Initial OpenAPI 3.1 contract for the existing v1 services, provider callbacks,
  postal-station control API, discovery pages, and process probes.
- Include all 15 workflow capability schemas and outcome transitions from the
  executable DAG contracts, plus the executable rendering request schema.
- Describe multipart ingestion and archive snapshots, payment negotiation,
  pagination, binary responses, deployment-dependent authentication, and aliases.
- No runtime API behavior changed by this initial specification.
