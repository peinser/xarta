# HTTP API v1 contract revisions

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
