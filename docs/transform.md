# Document transform capability

`transform` performs deterministic document-representation work inside a flow. The public DAG contract describes the requested operation; Gotenberg is the current internal execution engine and is not part of the flow profile.

Exactly one of `convert`, `merge`, or `split` must be present on a transform node.

## Convert

```json
{
  "kind": "transform",
  "convert": {
    "document": {"source": "generate", "id": "11111111-1111-1111-1111-111111111111"},
    "out": "22222222-2222-2222-2222-222222222222",
    "content_type": "application/pdf"
  }
}
```

V1 converts these declared input content types to `application/pdf` through Gotenberg's LibreOffice route:

- `text/plain`
- `text/csv`
- `application/rtf`
- `text/rtf`
- `application/msword`
- `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `application/vnd.ms-excel`
- `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- `application/vnd.ms-powerpoint`
- `application/vnd.openxmlformats-officedocument.presentationml.presentation`
- `application/vnd.oasis.opendocument.text`
- `application/vnd.oasis.opendocument.spreadsheet`
- `application/vnd.oasis.opendocument.presentation`

Content-type parameters such as `charset` are ignored when selecting the converter. Xarta does not sniff file content. Convert preserves the source `document_type` and metadata because it changes representation rather than business identity.

## Merge

```json
{
  "kind": "transform",
  "merge": {
    "documents": [
      {"source": "generate", "id": "11111111-1111-1111-1111-111111111111"},
      {"source": "generate", "id": "22222222-2222-2222-2222-222222222222"}
    ],
    "out": "33333333-3333-3333-3333-333333333333"
  }
}
```

Merge accepts two to 25 PDF inputs. The array order is the output page order. Xarta sends deterministic filenames (`000001.pdf`, `000002.pdf`, ...) because Gotenberg's PDF merge route orders inputs by filename.

A merged representation does not inherit `document_type` or metadata from an arbitrary source. Assign resulting business semantics explicitly when archiving it.

## Split

```json
{
  "kind": "transform",
  "split": {
    "document": {"source": "generate", "id": "11111111-1111-1111-1111-111111111111"},
    "outputs": [
      {"pages": {"start": 1, "end": 3}, "out": "22222222-2222-2222-2222-222222222222"},
      {"pages": {"start": 4, "end": 4}, "out": "33333333-3333-3333-3333-333333333333"}
    ]
  }
}
```

Split accepts one PDF and one to 25 statically declared outputs. Each output selects one contiguous, one-based page range with `start <= end`. Output UUIDs must be unique. Xarta does not expose Gotenberg/pdfcpu/QPDF/PDFtk range syntax and does not create dynamic DAG fan-out.

V1 executes one Gotenberg split request per missing declared output with `splitUnify=true`. This deliberately avoids provider ZIP output and provider-generated filename association. If large split fan-out becomes a real workload, batching can be added behind the same public contract.

Split outputs do not inherit the source `document_type` or metadata because a page fragment is not necessarily the same business document.

## Outputs and redelivery

Transform outputs are ordinary temporary generated documents. Downstream nodes reference them with:

```json
{"source": "generate", "id": "<transform output UUID>"}
```

There is no separate `source: transform` storage model.

Temporary output storage is immutable. On JetStream redelivery, Xarta reuses an already-persisted output UUID instead of invoking Gotenberg again. For a partially completed split, persisted outputs are reused and only missing outputs are generated. A concurrent create race also reuses the persisted winner after validating its expected content type and semantic metadata.

Gotenberg may produce byte-different but equivalent PDFs across independent executions because PDF generation can include timestamps. Xarta therefore never replaces an already-persisted transform output and does not claim byte-level determinism across independent provider calls.

## Execution characteristics

- **Execution mode:** synchronous. The final outcome is known before the JetStream handler returns.
- **PostgreSQL workflow state:** none. JetStream owns delivery, retry, acknowledgement, and deterministic successor publication.
- **External business side effect:** none. Gotenberg returns transformed bytes and creates no business record that Xarta must reconcile.
- **Retry safety:** safe before persistence. A timeout, connection failure, HTTP 429, or provider 5xx is retryable because repeating the transformation does not duplicate an external business action.
- **Deterministic rejection:** provider 4xx responses are permanent request failures. Raw provider response bodies are not copied to workflow errors or logs.
- **Callbacks/reconciliation:** none.
- **Exactly-once:** not claimed. ACK loss can redeliver a completed task; immutable output reuse makes that replay safe.

## Size limits

`TRANSFORM_MAX_BYTES` is one service-level bound. It limits one convert input, one split input, cumulative merge input bytes, and each returned provider output. Merge retrieves inputs sequentially so the process can stop when the cumulative limit is exceeded rather than loading all 25 documents first.

The default is 50 MiB. Raise it deliberately for deployments that accept larger office/PDF documents.

## Gotenberg

The current implementation uses:

- `POST /forms/libreoffice/convert` for conversion;
- `POST /forms/pdfengines/merge` for PDF merge;
- `POST /forms/pdfengines/split` for PDF split.

The transform service is configured with `TRANSFORM_GOTENBERG_ENDPOINT` and `TRANSFORM_GOTENBERG_TIMEOUT`. A flow cannot choose or address the provider.

Gotenberg is an internal document processor and should not be exposed through Xarta ingress. Deploy it with current upstream security fixes and restrict its outbound network access/linked-content behavior according to Gotenberg's deployment guidance. Xarta does not unpack OOXML/ODF documents to duplicate those provider-side protections.

## Deployment

Helm advertises `transform` to Intake only when `services.transform.enabled=true`. The transform worker mounts temporary storage and requires NATS, but no PostgreSQL workflow dependency.

A minimal values override is:

```yaml
services:
  transform:
    enabled: true
    settings:
      endpoint: http://xarta-template-engine-gotenberg:3000
      timeout: 30
      maxBytes: 52428800
      nats:
        workers: 10
```

If `settings.endpoint` is omitted, the chart uses `http://<release>-template-engine-gotenberg:3000`.
