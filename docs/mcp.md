# Model Context Protocol

## Purpose

Xarta exposes a dedicated Model Context Protocol service for agents that consult document
types, construct workflows, and retrieve archived results. The service uses the official Python MCP SDK's stateless
Streamable HTTP transport at `POST /api/mcp`. JSON responses are used instead of SSE
because the current tools do not require server-to-client requests or notifications.

MCP is a protocol facade over the versioned document-type, intake, archive, UBL, and Peppol HTTP APIs.
It owns no domain registry, pricing engine, payment server, execution path, archive
storage, or workflow state. Each service remains authoritative for its domain; intake
remains authoritative for flow validation, profile compilation, pricing, x402
requirements, settlement, and JetStream acceptance.

The compact machine-readable guide is available at `GET /agents.md`. The styled
`GET /agents` page is for people; MCP clients should connect directly to `/api/mcp`.

## Tools

Read-only discovery and preparation tools:

- `list_document_types` lists deployed document types and their public rendering defaults.
- `get_document_type` returns one document type's current template configuration and
  defaults.
- `check_document_type` validates generic render-request structure and resolves current
  document-type defaults without rendering. Its `validation_scope` is `render-request`;
  business-data semantics are deliberately not validated yet.
- `get_capabilities` returns only capabilities deployed on the target intake service,
  a recursive Draft 2020-12 JSON Schema for complete flows, each node's description,
  accepted fields and outcomes, valid example, runtime-only constraints, graph
  composition rules, and whether pricing is enabled.
- `list_flow_profiles` lists exact server-owned profile contracts.
- `get_flow_profile` returns one exact profile and its typed inputs.
- `prepare_flow` validates and normalizes a complete caller-authored DAG without
  executing it, and returns its selling price when pricing is enabled.
- `prepare_profile` compiles, validates, and prices an exact profile without executing
  it.
- `get_archived_document` returns current or exact-version metadata and, when content is
  available and within the configured size limit, an MCP resource link pinned to the
  immutable version.
- `list_archived_document_versions` returns cursor-paginated immutable version history.
- `list_ubl_validation_profiles` lists the UBL validator's profiles, releases, and checksums.
- `validate_ubl_schema` validates an XML string against the UBL schemas without storing it.
- `validate_ubl_business_rules` validates the schema and business rules; the default
  profile is `peppol-bis-billing-3`, including its Belgian identifier rules.
- `check_peppol_participant_registration` performs direct SML/SMP discovery using the
  four-digit ICD `scheme` and participant `identifier`.

The three UBL tools are advertised only when `MCP_UBL_BASE_URL` is configured; the
registration tool requires `MCP_PEPPOL_BASE_URL`. These read-only APIs are separate
from DAG capabilities. When deployed, the `ubl` editing node is also discoverable
through `get_capabilities` and executable through the existing flow tools.

### Validation and discovery results

Validation tools accept `xml` as a Unicode string, sent as UTF-8 XML with no JSON
wrapper. Its XML declaration must declare UTF-8 or omit the encoding. Input size is
checked in UTF-8 bytes before forwarding. `validate_ubl_business_rules` additionally
accepts `profile`; call `list_ubl_validation_profiles` to discover available profiles.
The UBL service remains authoritative for all XML/schema/business-rule decisions.

```json
{"xml": "<Invoice xmlns=\"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2\">...</Invoice>", "profile": "peppol-bis-billing-3"}
```

Registration tool arguments:

```json
{"scheme": "0208", "identifier": "0308357159"}
```

All four tools preserve the normal MCP proxy envelope `{"status": 200, "body": {...}}`.
Inspect the body: validation failures can have HTTP 200 and `valid: false`; engine
unavailability retains HTTP 503 and `valid: null`. Registration retains
`registered: true`, `false`, or `null`, along with `status`, `definitive`, `retryable`,
and the complete evidence list. **Indeterminate is never converted to unregistered.**
An MCP-to-service transport failure is a tool error, not a fabricated validation or
registration conclusion. No automatic retry, storage write, or submission is made.

All four tools are read-only, non-destructive, and idempotent in side effects. The
registration tool is marked open-world because its service contacts DNS/SMP servers;
the validation tools run against local pinned rules. Registration does not prove
Access Point liveness or support for a particular invoice type. See
[UBL validation](ubl-validation.md) and [Peppol discovery](peppol-discovery.md).

Execution tools:

- `submit_flow` submits a caller-authored DAG to generic intake.
- `submit_profile` submits typed inputs to one exact profile version.

Profiles are optional. Agents should inspect capabilities before constructing a custom
graph, use only advertised node kinds and outcomes, assign an explicit flow UUID, and
prepare the graph before submission. MCP currently accepts JSON document references; it
does not expose generic intake's multipart upload mode.

The two submission tools are marked destructive, non-idempotent, and open-world. They
can initiate irreversible provider operations such as external delivery. Tool annotations
are hints for MCP clients, not authorization controls.

## Archive Resources

Archive content is exposed through the resource template
`xarta-archive://{archive}/documents/{document_id}/versions/{version_id}/representations/{representation_id}`.
The metadata tool always resolves a `HEAD` lookup to an exact version and its default
representation before constructing this URI. Resource reads verify persisted size, SHA-512,
and content type before returning bytes. The official SDK transports those bytes as a base64
MCP blob.

The default raw-content limit is 4 MiB and the hard configuration maximum is 8 MiB.
Oversized and metadata-only representations remain discoverable but do not receive a
resource link. MCP never receives storage credentials or reveals backend names, revisions,
keys, buckets, or filesystem paths.

## x402

Preparation reports `{ "enabled": false }` when intake pricing is disabled. When x402
intake protection is enabled it reports the current pricing revision, selling price,
currency, and quote expiry. Internal cost estimates are not exposed.

An unpaid submission result contains:

```json
{
  "status": 402,
  "body": {"error": "Payment is required for this intake."},
  "payment_required": "<official x402 v2 PAYMENT-REQUIRED value>"
}
```

The agent authorizes those exact terms and retries the identical tool arguments with the
resulting `payment_signature`. MCP forwards that value as `PAYMENT-SIGNATURE` without
decoding or logging it. Intake authenticates the signed Xarta challenge and request
fingerprint, settles through the configured facilitator, and only then accepts work. A
successful result contains `payment_response`, normalized payment evidence, and status
`200`. Unpriced intake acceptance returns status `202`.

MCP never receives wallet private keys and does not create payment records. It must not
log tool arguments because a submission retry can contain an authorization payload.

## Execution And Retry Semantics

The MCP tool handler is synchronous as a protocol adapter: its final proxy result is
known before the handler returns. Document workflow execution remains governed by the
intake DAG and can continue asynchronously after acceptance. MCP creates no PostgreSQL,
NATS, outbox, attempt, outcome, or routing state.

Idempotency scope and expiry:

- MCP has no independent idempotency cache or replay window.
- An explicit flow ID gives intake deterministic flow, node, and task identities.
- JetStream duplicate suppression applies only for its configured duplicate window;
  execution admission handles active or completed deterministic tasks after that window.
- A payment identifier is not an indefinite replay key. A new valid authorization can
  purchase a new execution.

Duplicate delivery behavior:

- Repeating a prepared unpaid request with the same flow ID follows intake's normal
  deterministic duplicate behavior.
- Reusing or replacing payment authorization is interpreted only by intake and the x402
  facilitator. MCP does not claim exactly-once external side effects.

Retry safety and ambiguity:

- Discovery, archive reads, and preparation tools are safe to retry.
- Submission is safe to retry only according to the returned intake/x402 result and with
  an unchanged flow ID and request body.
- A connection failure before MCP receives an intake response is reported as intake
  unavailable; the outcome can be unknown.
- Facilitator timeout or malformed settlement evidence remains the uncertain boundary
  documented in [x402](x402.md).
- A crash after on-chain settlement but before JetStream acceptance is not recovered by
  MCP. Xarta intentionally has no standalone public-path payment ledger.

MCP has no callback or reconciliation support of its own. Provider callbacks and tracked
reconciliation remain capabilities of the submitted workflow.

## Deployment And Security

The MCP process runs in its own Deployment and calls the versioned document-type, intake,
archive, and configured UBL/Peppol services over internal ClusterIPs. It has no database, NATS, storage, or
provider credentials. The
transport validates `Host` and browser `Origin` values using the configured ingress
hosts to prevent DNS rebinding.

Configuration:

```text
MCP_INTAKE_BASE_URL
MCP_DOCUMENT_TYPE_BASE_URL
MCP_ARCHIVE_BASE_URL
MCP_UBL_BASE_URL
MCP_PEPPOL_BASE_URL
MCP_REQUEST_TIMEOUT_SECONDS
MCP_VALIDATION_TIMEOUT_SECONDS
MCP_UBL_MAX_DOCUMENT_BYTES
MCP_ARCHIVE_MAX_RESOURCE_BYTES
MCP_ALLOWED_HOSTS
MCP_ALLOWED_ORIGINS
```

The two new base URLs are optional; leave them unset to omit their tools. Empty strings
are not valid URLs. `MCP_VALIDATION_TIMEOUT_SECONDS` defaults to 35 seconds, allowing
the UBL service's default 30-second deadline to produce its structured result. Keep it
above the deployed UBL deadline. Registration uses `MCP_REQUEST_TIMEOUT_SECONDS`;
keep that above the Peppol discovery total deadline when you want service-level evidence.
`MCP_UBL_MAX_DOCUMENT_BYTES` defaults to 10 MiB, accepts values from 1 to 10 MiB, and
can be lowered independently of the service's own limit.

All proxy clients share MCP's lifespan-managed HTTP session, including its configured
Xarta User-Agent. URLs are deployment-owned; tools do not accept arbitrary service URLs.
Read-service requests do not follow redirects when forwarding documents.

`make mcp` and the development Compose configuration point the new tools at the
standalone runtime. The split Compose stack has an optional `mcp` profile; to enable
registration there, also run the `peppol` profile and set
`MCP_PEPPOL_BASE_URL=http://peppol:8000/api/v1/peppol`. Helm supplies each optional URL
only when the corresponding service is enabled, with bounds under
`services.mcp.settings.validationTimeoutSeconds` and `ublMaxDocumentBytes`.

`services.mcp.enabled` requires the intake, document-type, and archive services. Public ingress is independently
controlled by `services.mcp.ingress.enabled` and is disabled by default. Xarta does not
yet enforce platform-wide authentication, scopes, or tenant ownership. As stated in
`KNOWN_LIMITATIONS.md`, do not expose MCP outside a trusted network until those controls
cover MCP, intake, document types, and archive ownership.

## Non-Goals

- no natural-language model runs inside the MCP service;
- no duplicated DAG validator or profile compiler;
- no independent price calculation or x402 settlement;
- no wallet custody or payment authorization creation;
- no multipart document upload through MCP;
- no template-specific business-data validation yet;
- no workflow status projection or provider callback handling;
- no exactly-once claim for external side effects.
