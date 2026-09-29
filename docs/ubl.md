# UBL document editing

The `ubl` capability edits an existing, unsigned UBL 2.1 Invoice or CreditNote and produces a new XML document in [temporary storage](temporary-storage.md). It is independent of Peppol delivery: the same output can be archived, emailed, uploaded, or submitted to Peppol.

## Contract

```json
{
  "kind": "ubl",
  "document": {"id": "11111111-1111-1111-1111-111111111111"},
  "operations": [
    {
      "action": "add_attachment",
      "id": "supporting-document-1",
      "document": {"id": "22222222-2222-2222-2222-222222222222"},
      "filename": "supporting-document.pdf",
      "description": "Supporting invoice details"
    }
  ],
  "out": "33333333-3333-3333-3333-333333333333",
  "on": {
    "success": [
      {
        "kind": "peppol",
        "document": {"id": "33333333-3333-3333-3333-333333333333"}
      }
    ]
  }
}
```

| Field | Meaning |
| --- | --- |
| `document` | Shared reference to the input XML. No `source` means `temporary`. |
| `operations` | Ordered list of 1–25 typed edits. Currently only `add_attachment` is supported. |
| `out` | New temporary document UUID, distinct from every temporary input, including attachments. |
| `on` | Standard outcome-to-successor mapping. |

The top-level input and every attachment accept the existing temporary, archive, render, and bundle source contracts. For exact archive content, supply `source`, `id`, `archive`, `version`, and optionally `representation`. No separate `source: ubl` exists. The `generate` node kind remains distinct from the `temporary` source name.

An attachment operation requires:

- `action: "add_attachment"`;
- `id`: a nonblank UBL business reference, at most 256 characters, distinct from the attachment's storage UUID;
- `document`: a source reference;
- `filename`: a nonblank basename, at most 255 characters, without directory separators or control characters;
- optional `description`: nonblank text, at most 1,024 characters.

Duplicate IDs in the request or existing `AdditionalDocumentReference` elements are rejected (comparison ignores surrounding whitespace). Existing references are never replaced implicitly. External URL attachments, removal/replacement, generic XPath patches, invoice-field changes, and UBL creation are not implemented.

## XML semantics and validation

`UBLEditor` works on a private XML tree; it does not reconstruct the invoice from a partial business model. Unrelated elements, unknown extension content, and existing attachments are retained. Serialization may change XML declarations, namespace prefix choices, and lexical formatting; byte preservation of the original XML is not promised. The original input artifact is unchanged.

Each attachment becomes:

```text
cac:AdditionalDocumentReference
  cbc:ID
  cbc:DocumentDescription                 (optional)
  cac:Attachment
    cbc:EmbeddedDocumentBinaryObject      (base64-encoded bytes)
      @mimeCode
      @filename
```

References are inserted in UBL schema order before project references/signatures/supplier information, not appended after invoice lines. Input and output both undergo full XSD validation against the bundled UBL 2.1 schemas. An explicit `UBLVersionID`, when present, must be `2.1`.

XML parsing disables entity expansion, DTD loading, network access, recovery, and huge-tree mode. DTD declarations and entity nodes are rejected, including declarations beyond the beginning of the document and non-UTF-8 encodings. XML DSig signatures, UBL signature elements, and UBL signature containers are rejected rather than silently invalidated by editing. Add attachments before XML signing. The existing `signature` service signs PDF attachments, not UBL XML.

The generic UBL editor accepts syntactically valid MIME types, including types outside Peppol's billing subset. MIME types come from resolved source metadata; parameters are stripped and case normalized. Empty attachments are rejected. Xarta does not infer MIME type from the filename or sniff attachment bytes. The six-type Peppol subset is enforced by the official business-rule validator (`PEPPOL-EN16931-CL001`), not hard-coded as a general UBL limitation.

Editing performs structural XSD validation. To validate EN 16931 and Peppol BIS Billing 3 rules, including the Belgian enterprise-number rule, use the [UBL validation APIs](ubl-validation.md). A downstream Peppol service/provider still applies its delivery validation to the edited document. Its routing identities and digest are derived from the edited output.

## Atomicity, retries, and provenance

The node publishes one output only after every operation succeeds and the final XML validates. An unsuccessful edit produces no output document. Input snapshot objects can remain after failure and are subject to temporary cleanup.

Before editing, the worker resolves inputs sequentially and writes their bytes under content-addressed temporary keys. A create-only manifest at `ubl/<out>/manifest.json` pins the input set and a fingerprint of the input references, operations, and editor contract revision. The first successfully published manifest wins concurrent resolutions. A retry after that boundary uses the pinned bytes rather than re-reading a mutable archive head or re-rendering an attachment.

Before the manifest exists, a failed attempt may resolve live inputs again. Partially written input snapshots are not treated as a committed input set. Concurrent losers load the winning manifest; their unused content-addressed objects can later be cleaned up.

The result uses `application/xml`, preserves the original `document_type` and application metadata, and replaces the reserved `_xarta_ubl` metadata entry with provenance for this edit. Provenance records the request fingerprint, source references, input content types, and SHA-256 digests. Resolved archive version and representation IDs are recorded **only for archive inputs**. Temporary inputs have no version/representation fields. Original application metadata is not duplicated into the provenance input list.

Persistence is create-only: identical retries succeed; reusing `out` for a different request or different output fails permanently. The worker can reconstruct a missing metadata completion marker after a partial output write. It never overwrites a different binary. Byte-identical replay assumes the same editor and XML-library implementation, so keep them stable for in-flight work.

The idempotency scope is the output UUID in the configured temporary backend. Its lifetime is the retention of the manifest, snapshots, and output. Cleanup is object-based, not transactional across this set; configure retention beyond the replay window. Missing pinned input objects fail retrieval rather than silently fetching new input versions. Do not replay expired work with an occupied output UUID; create a new operation/output or restore the complete snapshot set.

## Execution and errors

- **Mode:** synchronous JetStream consumer (`UBLNATSModel`); no PostgreSQL workflow state.
- **Success:** one `success` emission after a complete output is persisted.
- **Failures:** domain errors become `PermanentError` with stable `ubl_*` classifications and follow the shared synchronous failure routing/dead-letter rules. Schema messages do not expose invoice contents.
- **Retryable boundary:** source/storage transport errors propagate to the shared retry machinery. Repeating an edit performs no external business action. Source resolvers such as render may perform their own production requests before a manifest is committed.
- **Duplicate delivery:** reuses the committed input set and performs create-only output writes. ACK occurs through the shared consumer only after successor publication.
- **Callbacks/reconciliation:** none. At-least-once execution is supported; exactly-once execution is not claimed.

Error classifications include `ubl_invalid_document`, `ubl_unsupported_document`, `ubl_signed_document`, `ubl_attachment_conflict`, `ubl_unsupported_attachment`, `ubl_size_limit`, `ubl_output_conflict`, and `ubl_input_corrupt`.

`success` and `failure` have maximum emission cardinality one for pricing. Configure a deployment pricing binding/rule for `ubl` when pricing is enabled; registering the capability does not install a price.

## Deployment

The official OASIS UBL 2.1 Invoice/CreditNote XSDs and all common dependencies are bundled under `src/xarta/ubl/resources/xsd/`, with upstream notices and a SHA-256 manifest. There is no schema mount, override, or runtime download. The service never resolves schemas supplied by a document.

Required settings:

| Setting | Meaning |
| --- | --- |
| `UBL_MAX_BYTES` | Positive byte limit, default 10 MiB. Bounds total resolved input bytes and final serialized XML independently. Base64 expansion counts toward the output limit. |
| `TMP_STORAGE` / `TMP_STORAGE_CONFIG_PATH` | Same temporary backend used by producers and downstream consumers. |
| `NATS_SERVERS` / `NATS_WORKERS` | Standard synchronous-worker connection/concurrency settings. |
| `ARCHIVE_SERVICE_ENDPOINT`, `BUNDLE_SERVICE_ENDPOINT`, `RENDER_SERVICE_ENDPOINT` and corresponding `_TIMEOUT` settings | Shared source resolver configuration. |

Invalid limits fail startup. The final-output bound includes XML overhead and base64 expansion; a binary payload that fits the input bound can still exceed the output bound. Source retrieval currently materializes each complete source before checking its size, so this is an acceptance bound, not a streaming memory limit.

Run the worker with the standard bootstrapper after setting the required environment:

```console
uv run --frozen python -m xarta.bin.services --run ubl=v1 --run ubl=latest
```

Include `ubl` in `INTAKE_CAPABILITIES` only where that worker is running. The GET routes `/api/v1/ubl/` and `/api/ubl/` are empty service endpoints; submit edits through Intake DAGs or flow profiles. Separate HTTP endpoints provide [schema and business-rule validation](ubl-validation.md).

Helm example using the bundled schemas and rules:

```yaml
services:
  ubl:
    enabled: true
    settings:
      maxBytes: 10485760
      validationTimeout: 30
      validationConcurrency: 2
      nats:
        workers: 10
```

The chart supplies source endpoints, shares temporary storage, and advertises `ubl` to Intake only when enabled.

## Implementation and verification

- `protocol/dag/ubl`: `UBLNode`, `UBLRequest`, and `UBLAddAttachment` validate intent without I/O.
- `ubl/editor.py`: `UBLEditor`, `UBLAttachment`, and `UBLError` implement storage-independent XML operations and schema validation.
- `services/v1/ubl`: resolves and pins inputs, executes edits, persists outputs, and translates domain failures.

Run `uv run --frozen pytest tests/ubl`. Offline tests use the actual vendored OASIS schemas and Peppol business rules.

The suite covers Invoice/CreditNote attachment structure, ordering, extension preservation, malformed XML and signed-input rejection, duplicate IDs, schema failures, encoded-size limits, reference parsing, output collisions, pinned archive inputs, concurrent delivery, and recovery after partial writes.
