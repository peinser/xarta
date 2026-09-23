# Adding DAG Capabilities

This guide covers the complete path for adding a first-class DAG node and its
implementations. A node is not complete when its protocol class parses. Intake,
execution, routing, pricing, deployment, retries, storage, and operator documentation
must agree on the same semantics.

## Start With The Capability Contract

Define the business operation independently of any provider. The node specification
must contain only application-level intent and stable references needed by every
implementation. Provider credentials, URLs, account IDs, and implementation-specific
retry controls belong in server-side adapter configuration.

Decide and document:

- the node `kind`;
- required and optional fields;
- every finite semantic outcome;
- the maximum number of times each outcome can emit per node activation;
- deterministic input and output identities;
- whether outputs use temporary storage, archive storage, or an external provider;
- whether an output can be reproduced byte-for-byte after redelivery;
- which failures are semantic outcomes, retryable transport failures, uncertain
  outcomes, or permanent execution failures.

Use explicit document sources for document inputs. For example, a bundle built from an
archived version declares `source: archive`, the archive name, document ID, and exact
version. Do not resolve "current" when a predecessor has already emitted a concrete
version ID; doing so introduces a race with later versions.

Shared protocol and domain validators raise `ValueError` or another domain exception.
They must not import Sanic or raise HTTP exceptions. The HTTP route translates the
validation error once into `BadRequestError`.

## Define And Register The Node

Add the protocol type under `src/xarta/protocol/dag/<kind>/`. Follow an existing node:

- subclass `Node`;
- define `KIND` and `OUTCOMES`;
- parse IDs into `UUID` values;
- validate bounded collections and fields during construction;
- provide `dict()` and `fromdict()` round trips;
- keep `interpret()` free of I/O; it may construct validated domain requests or
  document sources.

Register the parser in `src/xarta/protocol/dag/utils.py::_NODE_PARSERS`. This registry is
used by both intake and `NodeTask` deserialization. A producer-only parser is not
sufficient: workers must reconstruct the same node from JetStream.

Add the node fields to `src/xarta/flow_profiles/compiler.py::_NODE_FIELDS` if profiles
may emit the capability. Test profile compilation separately from direct DAG parsing.

Add focused protocol tests for:

- parse/serialize/parse equality;
- parent and successor preservation;
- every invalid field and bound;
- allowed and rejected outcomes;
- inclusion in `supported_node_kinds()`;
- deterministic IDs and output representations.

## Choose The Execution Model

Execution mode is selected by the deployed implementation or adapter, not by the node.
The same provider-neutral node can be synchronous for one adapter and tracked for
another.

### Synchronous

Use `ExecutionMode.SYNCHRONOUS` when the final outcome is known before the JetStream
handler returns. Typical examples are deterministic local transformations and external
requests whose response fully resolves the operation.

A synchronous implementation:

- subclasses `SanicNATSSynchronousRequestsConsumerModel`, or is selected as synchronous
  by a mixed consumer;
- performs the work and publishes every deterministic successor before ACK;
- creates no PostgreSQL executions, attempts, outcomes, branch activations, outbox
  entries, tracked operations, or other workflow-routing state;
- raises `TemporaryError` only when repeating the handler is safe under the documented
  idempotency contract;
- produces byte-identical immutable-storage output on redelivery;
- accepts that ACK loss can repeat work, and documents any external duplicate effect.

Deterministic task identity does not imply exactly-once external side effects. JetStream
deduplication is bounded by its duplicate window, and an ACK or successor-publication
failure can redeliver a completed handler.

### Tracked

Use `ExecutionMode.TRACKED` when the operation remains pending after the handler returns,
including provider callbacks, delayed delivery states, or reconciliation.

A tracked implementation:

- admits the task through `PostgresTrackingStore` before invoking the adapter;
- checkpoints the selected adapter, configuration revision, provider account scope,
  idempotency key, and any provider identity needed for reconciliation;
- returns `waiting_feedback` while the external operation remains open;
- normalizes callbacks and polling into one reducer;
- commits operation state and immutable outcome events atomically;
- publishes deterministic successors directly to JetStream after commit;
- handles expired execution-attempt leases without creating duplicate logical
  operations.

Do not use tracked execution merely because an HTTP API returns `202`. If Xarta waits
inside the handler and knows the final result before return, it is synchronous. If the
handler returns while the provider still owns pending work, it is tracked. Do not model
a synchronous successor as PostgreSQL handoff work.

Postal is an explicit example of why mode belongs to the adapter. `PostalNode` contains
one provider-neutral mailpiece intent and no execution flag. The Local Postal Adapter's
required mode is tracked because physical production, handover, and registered-carrier
feedback outlive the initial handler. A future package-export implementation could be
synchronous without changing the node. The current repository registers the postal
protocol but does not yet wire adapter dispatch or a postal consumer; see
`docs/postal.md`.

## Implement Adapters

Keep the generic worker provider-neutral. Use `AdapterRegistry` and factories for
multiple implementations. Factories validate configuration without network calls and
construct adapters from a pinned revision.

Each adapter must document:

- selected execution mode and why;
- idempotency-key scope and expiry;
- the exact ambiguous side-effect boundary;
- behavior after duplicate JetStream delivery;
- which errors are safe to retry;
- provider-account correlation scope;
- callback authentication and deduplication, when available;
- reconciliation support and terminal-state rules;
- what happens after the provider's idempotency window expires.

Destination names in a DAG are logical. Resolve the adapter and immutable configuration
revision server-side. Retain revisions while active tracked operations reference them.
Never persist credentials in workflow rows, messages, outcomes, or logs.

## Implement The Consumer

Place the worker with its service under `src/xarta/services/v1/<service>/nats.py` and
register it from the service blueprint's startup listener. Register a distinct NATS model
for each independent subject/durable pair; do not reuse one runtime for both jobs and DAG
requests.

Subjects follow:

```text
requests.<flow-id>.tasks.<node-execution-id>.<kind>
```

### Structured lifecycle logs

The shared NATS and tracking layers emit `capability_lifecycle_state_changed` for every
capability. Synchronous nodes record execution start and terminal resolution after their
deterministic successor publications complete. Tracked nodes additionally record attempt
start, transition to waiting feedback, operation preparation, uncertainty, and committed
callback or reconciliation reductions. Registry-backed services emit
`capability_adapter_selected` with the capability, adapter, immutable configuration
revision, and execution mode.

Every lifecycle record carries `flow_id`, `correlation_id`, `node_id`, and
`node_execution_id`. Tracked-operation records also carry `operation_id` and the pinned
adapter revision. `previous_version` and `next_version` distinguish committed reducer or
checkpoint updates that intentionally retain the same lifecycle state. Query UUIDs as
parsed JSON fields rather than indexing them as high-cardinality log labels. For example:

```logql
{app=~"$xarta_services"} | json | correlation_id="$correlation_id"
```

Lifecycle logging is allowlisted. Never add node payloads, tracked state, reducer updates,
outcome details or subject identifiers, provider event IDs, destination configuration,
credentials, document contents, addresses, or recipient identifiers. Domain services may
add narrower state-transition events when the generic lifecycle cannot express a safe
physical or provider state.

The request stream already covers `requests.>`. The consumer durable name normally
matches the capability kind. Return `CapabilityResult` with explicit
`OutcomeEmission` values. Do not select edges in the worker; the routing layer resolves
exact `Node.on[outcome]` entries.

For transformations that write temporary documents:

- require `TMP_STORAGE` in service configuration;
- mount/configure temporary storage in Helm;
- use an explicit output UUID from the node or derive it deterministically from the task;
- write the binary and metadata through `DocumentSourceResult.persist()`;
- make output bytes deterministic because storage is immutable and create-only;
- ensure a partial binary/metadata write can be retried safely.

Move blocking compression, signing, or parsing work to `asyncio.to_thread`. Bound input
counts and assess total-memory limits; a count limit alone does not bound bytes.

## Advertise Availability

Protocol support and deployment availability are separate. After registering the parser:

- add the capability to development `INTAKE_CAPABILITIES`;
- append it in Helm's intake deployment only when the responsible service is enabled;
- add every required Secret, ConfigMap, volume, endpoint, and environment variable to
  the service deployment;
- update the documented default manifest;
- run `make helm-verify`.

Intake must reject a valid protocol node when the deployment does not advertise its
capability. Test both accepted and unavailable nested nodes.

## Add Pricing Coverage

Pricing receives the compiled executable DAG. Add explicit outcome cardinality in
`src/xarta/pricing/graph.py`; unknown capability/outcome cardinality must continue to
fail closed.

Capability names in pricing bindings are dynamic, but each deployment's binding resolver
must select an adapter and configuration revision, and the pricing catalog must contain a
matching capability/adapter rate. There is no implicit zero-cost rate. Add a pricing test
that constructs the real node, traverses at least one outcome edge, resolves its binding,
and verifies cost aggregation.

Document whether callers select a destination. Local capabilities such as bundle usually
have no destination and use a server-selected internal adapter binding.

## Verification Checklist

Before considering a node complete, verify:

1. Protocol round trip and invalid input tests pass.
2. Intake accepts the advertised capability and rejects it when unavailable.
3. The consumer uses the intended synchronous or tracked execution path.
4. Every declared outcome routes only its exact edge.
5. Retry, redelivery, exhausted retry, and ACK-loss behavior are covered.
6. Deterministic successors and immutable outputs replay safely.
7. Synchronous execution leaves all PostgreSQL workflow tables empty.
8. Tracked execution persists and replays callback/reconciliation state correctly.
9. Pricing binding, cardinality, catalog lookup, and aggregate cost are tested.
10. Helm renders all required configuration and storage.
11. Logs contain flow, node, execution, outcome-event, and semantic subject identities
    without credentials or document contents.
12. `make verify` and `make helm-verify` pass.
13. A realistic end-to-end smoke test verifies output bytes and reports handler-level and
    wall-clock timing.

See `docs/tracked-capabilities.md`, `docs/temporary-storage.md`, and `docs/pricing.md` for
the detailed persistence, storage, and quotation contracts.

Postal-specific protocol, operational, and carrier boundaries are documented in
`docs/postal.md` and the linked adapter guides.
