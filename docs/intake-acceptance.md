# Intake acceptance and execution delivery

Named flow profiles compile to an ordinary executable DAG before capability validation
and before source persistence or NATS publication. Profile-specific intake accepts only
declared typed inputs. Inline DAG intake remains available for compatibility.

Profile node identities derive from the exact profile version and stable `node_key`;
inline identities retain structural-path behavior. Pricing and payment use
`PreparedIntake.semantic_fingerprint`, covering the compiled DAG and staged sources.

`POST /api/v1/intake/` parses and validates the complete flow before causing side effects. For multipart requests, every upload is persisted before intake publishes the root `NodeTask` directly to the `REQUESTS` JetStream subject. Intake waits for the JetStream `PubAck`; only then does it return `202`. A `202` therefore means that JetStream accepted the root task, not that execution started or completed.

NATS is a required intake dependency. Intake connects synchronously during startup through the normal Sanic NATS lifecycle, fails startup when NATS is unavailable, and reports not-ready if an established connection is lost. A publication failure returns `503 Service Unavailable`. The caller must retry the complete intake request; persisted uploads are immutable and accepting the same bytes again is safe.

The root subject is `requests.<flow-id>.tasks.<node-execution-id>.<kind>`. Its headers are `flow-id`, `flow-correlation-id`, and `Nats-Msg-Id`, with the root execution ID used as the message ID. Intake has no PostgreSQL registration, root admission record, transactional outbox, or background publication relay. All later DAG successors use the same direct deterministic JetStream handoff.

Paid intake follows the same execution architecture. Xarta authenticates the signed x402 challenge, settles, logs the transaction hash, and then publishes the root task directly. Payment does not create workflow state for synchronous nodes. The task carries bounded settlement admission evidence so that a later genuinely tracked node can persist it on its normal execution row before irreversible work. A crash after settlement but before JetStream acceptance is an intentional synchronous public-payment boundary; there is no standalone payment ledger from which Xarta can recover that obligation.

When a client supplies a flow ID, omitted correlation and DAG node IDs are derived deterministically. The root execution ID is derived from the flow and root node IDs. Retrying the same request therefore republishes the same payload, subject, and identifiers. JetStream suppresses the duplicate while its configured duplicate window covers the retry; after that window, worker execution admission must prevent a completed or actively leased execution from running twice.

Persisting uploads and publishing to JetStream cannot be atomic. A publish failure can leave uploads persisted without a root task, and a lost HTTP response can cause the caller to republish an already accepted task. Deterministic identities, JetStream duplicate suppression, immutable upload persistence, and execution-side admission make identical retries safe across those failure windows.

Intake no longer owns an admission ledger, so it cannot return `409 Conflict` when the same flow ID is submitted with different semantics. Such a conflict is detected and rejected by the consumer execution ledger only if that ledger implements semantic task validation. Without that validation, behavior for conflicting requests with the same flow ID is undefined; clients must treat a supplied flow ID as bound to one canonical flow definition.
