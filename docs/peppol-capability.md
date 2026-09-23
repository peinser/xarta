# Peppol Capability

## Contract

**A `PeppolNode` says only: submit this UBL document through Peppol.**

The node contains a `DocumentSource` and semantic `on` edges. Provider/environment
selection is deployment configuration. The UBL is authoritative for document kind,
business ID, issue date, `CustomizationID`, `ProfileID`, and sender/receiver
`EndpointID` values.

e-invoice.be and Recommand are tracked Peppol adapters. Neither provider defines the
limits or caller contract of the generic capability.

Initial scope is UBL 2.1 Invoice and CreditNote using Peppol BIS Billing 3 Profile 01. Billing-with-Response Profile 02 is rejected as `unsupported_profile` before provider state is created because this adapter does not expose the required Invoice Response lifecycle.

## Execution Order

1. Retrieve the exact document bytes and inspect the UBL with external entities, DTD loading, network access, recovery, and huge trees disabled.
2. Require the supported UBL type and Profile 01.
3. Resolve the UBL sender to its configured provider account. e-invoice.be lazily
   validates account identity; Recommand uses the configured company directly.
4. Perform provider read-only validation when supported; Recommand relies on Xarta's
   inspection plus submission-time provider validation.
5. Look up the UBL receiver participant and supported document/process where available.
6. Create a `TrackedOperation` containing the pinned service configuration revision, provider tenant ID, source identity, SHA-256, business identity, and sender/receiver.
7. Persist phase `creating`, then call `POST /api/documents/ubl` with the exact bytes.
8. Persist the returned DRAFT ID before any send request.
9. Persist phase `sending`, then call `/send` with all four endpoint query parameters from pinned UBL identity.
10. On `TRANSIT`, emit `submitted`, leave the operation open, set the node execution to `WAITING_FEEDBACK`, and ACK the NATS delivery.
11. Normalize callback and polling observations through the same reducer. `SENT` emits `delivery_confirmed`; `FAILED` emits `delivery_failed`; both resolve the operation.

Steps 7-11 describe e-invoice.be's staged submission. Recommand uses direct raw XML
submission and never creates a fictional draft. It checkpoints `submitting` before the
write, then durably stores the normalized send response and returned provider document ID
before reduction. `sentOverPeppol=true` maps to technical `delivery_confirmed`; a
definitive response that did not send over Peppol fails terminally rather than waiting
for polling.

e-invoice.be does not use UBL EndpointIDs as routing authority by default. The adapter always supplies `sender_peppol_scheme`, `sender_peppol_id`, `receiver_peppol_scheme`, and `receiver_peppol_id`. Provider inference from tax/company fields is never relied upon.

## Crash And Retry Safety

The operation exists before DRAFT creation. A lost DRAFT response leaves phase `creating`; redelivery does not create another DRAFT and emits `outcome_uncertain` unless deterministic provider reconciliation proves identity. Once a DRAFT ID is checkpointed, redelivery loads that ID and never creates another document.

A lost `/send` response is reconciled with `GET /api/documents/{id}` before another side effect. `TRANSIT`, `SENT`, and `FAILED` are authoritative observations. A remaining `DRAFT` is treated as ambiguous rather than automatically resent. While `TRANSIT`, Xarta never competes with provider delivery retries.

The service binding includes its configuration revision and provider account reference. Callback and reconciliation resolve that pinned identity, even after `current_revision` changes.

Recommand documents no idempotency or unique recovery key for direct send. A lost direct
send response therefore emits `outcome_uncertain` and is never blindly retried. Once its
response is checkpointed, retry replays that local observation without POST or GET.

## Outcomes

- `submitted`: e-invoice.be accepted responsibility and reports `TRANSIT`; nonterminal.
- `invalid_document`: local or provider UBL validation failed.
- `unsupported_profile`: the UBL process is outside the adapter's supported scope.
- `recipient_not_registered`: participant lookup definitively reported absence.
- `delivery_confirmed`: e-invoice.be reports `SENT`; this does not mean viewed, accepted, approved, or paid.
- `delivery_failed`: e-invoice.be reports terminal `FAILED`.
- `outcome_uncertain`: Xarta cannot establish whether an external side effect occurred.
- `execution_failed`: configuration, authentication, rate-limit, transport, or provider operation failed without implying recipient absence or delivery failure.

`OutcomeEvent.details` contains bounded, sanitized evidence such as provider document ID, provider state, validation rule, failure category, HTTP status, provider code, and message. It never contains raw UBL, credentials, authorization headers, raw provider payloads, or stack traces. DAG routing evaluates only `OutcomeEvent.outcome`.

`GET /api/v1/peppol/operations/{operation_id}` returns the pinned operation state
and immutable outcome history, including safe `details`, so operators can inspect a
terminal provider reason without relying on centralized logs. This observability
endpoint does not change DAG routing semantics.

## Feedback

The callback route is `POST /api/v1/peppol/callbacks/e-invoice-be`. Bounded JSON is parsed first only to obtain untrusted tenant/document routing hints. The complete event is then serialized with the provider's documented sorted-key Python JSON representation and its tenant-specific HMAC-SHA256 is checked in constant time before operation mutation. Stable provider event IDs deduplicate callbacks in the generic provider inbox. State changes and outcome events commit atomically; deterministic successors publish directly to JetStream before callback acknowledgement and replay on callback retry.

Leased periodic reconciliation loads the provider document using the operation's pinned configuration revision and provider tenant, then feeds the same `PeppolUpdate` reducer as callbacks. Temporary provider read failures leave semantic state unchanged and use bounded backoff.

Recommand callbacks use raw-body HMAC authentication and a durable JOBS-stream hint so
HTTP acknowledgement follows durable handoff. The consumer treats callbacks from the
dedicated `Document sent` rule as provider evidence and performs no provider retrieval.
Unlike e-invoice.be's staged operations, Recommand's direct operations do not poll
provider documents. See
[`docs/providers/recommand.md`](providers/recommand.md) for its exact boundary.
