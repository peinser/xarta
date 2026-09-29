# UBL and Belgian Peppol billing validation

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/ubl/validate/schema` | Secure XML parsing and UBL 2.1 Invoice/CreditNote XSD validation. |
| `POST /api/v1/ubl/validate/business-rules?profile=peppol-bis-billing-3` | XSD validation, followed by the complete EN 16931 UBL and Peppol BIS Billing 3 rules. |
| `GET /api/v1/ubl/validation/profiles` | Profiles, pinned releases/checksums, document kinds, and validation stages. |

The `/api/ubl/` alias exposes the same endpoints. Send XML directly with
`Content-Type: application/xml` or `text/xml`. Nothing is stored or sent to an
external validator. The default profile is `peppol-bis-billing-3`.

```console
curl -H 'Content-Type: application/xml' --data-binary @invoice.xml \
  http://localhost:8000/api/v1/ubl/validate/schema
curl -H 'Content-Type: application/xml' --data-binary @invoice.xml \
  'http://localhost:8000/api/v1/ubl/validate/business-rules?profile=peppol-bis-billing-3'
```

## Exact coverage

The bundled release is **Peppol BIS Billing 3, May 2026**. Both official compiled
Schematron validators run: `CEN-EN16931-UBL.xslt` and `PEPPOL-EN16931-UBL.xslt`.
SaxonC supplies XSLT 2.0 support; the rules are not approximated using Python field
checks or lxml's XSLT 1.0 engine.

This includes Belgian Peppol billing validation. In particular,
`PEPPOL-COMMON-R043` checks ten-digit Belgian enterprise identifiers and their
modulo-97 checksum wherever the official rule applies to scheme `0208`, including
seller and buyer identifiers. The entire arithmetic, tax, required-field, code-list,
and Peppol rule sets run as well. Country-dependent rules use their upstream XPath
contexts. Xarta does not invent a separate Belgian CIUS, force every counterparty
to be Belgian, or equate technical validation with legal/tax compliance.

The six attachment types are exhaustive **for the current Peppol BIS subset**,
not UBL in general: PDF, ODS, XLSX, JPEG, PNG, and CSV. The generic UBL editor accepts
other valid MIME types. A `text/plain` attachment therefore passes UBL XSD validation
but fails `PEPPOL-EN16931-CL001` at the business-rule endpoint.

Neither endpoint checks recipient registration or cryptographic XML signatures.
Structurally valid signed XML can be validated; editing rejects signed input.

## Results and status codes

A completed validation returns **HTTP 200 whether it passes or fails**:

```json
{
  "valid": false,
  "document_kind": "Invoice",
  "checks": {"xml": "passed", "xsd": "passed", "business_rules": "failed"},
  "issues": [{
    "stage": "peppol",
    "rule": "PEPPOL-COMMON-R043",
    "severity": "fatal",
    "message": "Belgian enterprise number MUST be stated in the correct format.",
    "location": "<XPath from the official SVRL report>"
  }],
  "ruleset": {"id": "peppol-bis-billing-3", "release": "2026.05", "sha256": "<bundle digest>"},
  "truncated": false
}
```

Warnings do not make `valid` false; fatal/error findings do. Up to 1,000 issues are
returned. `truncated` signals omitted issues; validity still considers every finding.
Malformed/unsafe XML and XSD errors stop subsequent stages, which are marked
`not_run`. Schema-only validation has no business-rule result or ruleset.

| HTTP status | Meaning |
| --- | --- |
| 400 | Unsupported profile. |
| 413 | Input exceeds `UBL_MAX_BYTES`. |
| 415 | Unsupported request media type. |
| 503 | Business-rule engine failed or timed out: `valid: null`, `status: "indeterminate"`. This is not an invalid-document conclusion. |

Diagnostics are returned to the caller; document contents are not logged by the
validator. DTDs/entities are rejected before Saxon parses the document. Caller-supplied
XSLT, schema locations, external functions, and resource URLs are never executed.

## Runtime and configuration

Business validation uses bounded isolated subprocesses. Its deadline includes the
admission queue. Cancellation/timeout kills and reaps the child instead of leaving
uncancellable native work running in a thread. XSD validation runs off the event loop
and serializes access to each shared schema's diagnostic log.

| Setting | Default |
| --- | --- |
| `UBL_MAX_BYTES` | 10 MiB |
| `UBL_VALIDATION_TIMEOUT` | 30 seconds, including queue wait |
| `UBL_VALIDATION_CONCURRENCY` | 2 business-validation subprocesses per service process |

`.dev/compose.yml` and `.dev/compose.scenarios.yml` configure these bounds. Helm equivalents are under
`services.ubl.settings`. The validation APIs make no outbound HTTP calls and create
no NATS workflow or PostgreSQL state.

Assets, license notices, upstream examples, immutable versions, and checksums live
under `src/xarta/ubl/resources/` and ship with the package. Run
`python tools/vendor_ubl.py --check` for offline integrity verification. Maintenance
imports are explicit and pinned; rules never auto-update at runtime.

## Sources

- [Official UBL 2.1 distribution](https://docs.oasis-open.org/ubl/os-UBL-2.1/UBL-2.1.zip)
- [Peppol BIS Billing 3](https://docs.peppol.eu/poacc/billing/3.0/)
- [Belgian enterprise-number rule](https://docs.peppol.eu/poacc/billing/3.0/rules/ubl-peppol/PEPPOL-COMMON-R043/)
- [Peppol MIME subset](https://docs.peppol.eu/poacc/billing/3.0/codelist/MimeCode/)
- [Pinned compiled rule distribution](https://github.com/phax/phive-rules/tree/000e6fb9aba9cd6ca4586ef4174dc8bcd5dc9c55/phive-rules-peppol/src/main/resources/external/schematron/openpeppol/2026.5/xslt)
