# Model Context Protocol

## Purpose

Xarta exposes a dedicated Model Context Protocol service for agents that consult document
types, construct workflows, and retrieve archived results. The service uses the official Python MCP SDK's stateless
Streamable HTTP transport at `POST /api/mcp`. JSON responses are used instead of SSE
because the current tools do not require server-to-client requests or notifications.

MCP is a protocol facade over the versioned document-type, intake, and archive HTTP APIs.
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
and archive services over internal ClusterIPs. It has no database, NATS, storage, or
provider credentials. The
transport validates `Host` and browser `Origin` values using the configured ingress
hosts to prevent DNS rebinding.

Configuration:

```text
MCP_INTAKE_BASE_URL
MCP_DOCUMENT_TYPE_BASE_URL
MCP_ARCHIVE_BASE_URL
MCP_REQUEST_TIMEOUT_SECONDS
MCP_ARCHIVE_MAX_RESOURCE_BYTES
MCP_ALLOWED_HOSTS
MCP_ALLOWED_ORIGINS
```

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
