# Tracked Capabilities

Xarta separates four execution concepts:

- `Node` is the immutable application DAG specification. Its ID identifies the specification, not a run.
- `NodeExecution` is one durable activation of a node in one flow. Repeated outcomes may create multiple executions for the same node ID.
- `ExecutionAttempt` records one worker attempt to advance an execution.
- `TrackedOperation` is the external operation that can remain open after an attempt and emit outcomes over time.

An `OutcomeEvent` is immutable. `OutcomeEvent.outcome` is the finite semantic fact used for routing, `subject` identifies an optional semantic entity, and `details` is Xarta's generic immutable JSON context containing bounded, sanitized evidence. Routing uses only the exact `Node.on[outcome]` edge; DAG routing never evaluates `details`, and details are not copied into `TriggerContext`. For example, email may emit `mailbox_full` with SMTP code `550` and enhanced status `5.2.2`, while Peppol may emit `delivery_failed` with provider `e-invoice.be`, provider state `FAILED`, and a provider-native code. Every `(outcome_event_id, successor_node_id)` pair derives one deterministic JetStream task identity. Different outcome events can activate the same successor specification independently.

Tracked capability reducers own semantic state and aggregate resolution. Emitting an outcome does not resolve an operation. This permits recipient or protocol feedback to activate branches while the originating execution remains `waiting_feedback`.

Provider callbacks and reconciliation can become normalized capability updates. The tracked service deduplicates provider event IDs and commits operation state and outcomes atomically. It then publishes every deterministic successor directly to JetStream before acknowledging the callback or reconciliation work. A retry replays successors from immutable outcome events without rerunning the reducer.

When a flow was admitted through x402, its task carries bounded settlement evidence. The first genuinely tracked execution stores that admission JSON on `node_executions` before adapter work and propagates it to successors. This is workflow provenance explaining why the execution was admitted, not a payment ledger. Synchronous nodes propagate the evidence without creating tracking rows.

JetStream is the scheduler. Task subjects and `Nats-Msg-Id` use the deterministic node execution ID. A receiving adapter selects synchronous execution or PostgreSQL tracking before admission; the producer never creates database state for its successor. ACK means the handler completed its work and successor publications, not that the external business operation succeeded.

Migration `00021.direct-successor-routing` removes the former execution outbox and branch-activation tables. It intentionally aborts if unpublished handoffs or legacy branch identities exist; those flows must be drained before deployment so the migration cannot silently lose or renumber work.

Worker admission is serialized by the tracking store. A scheduled execution, or an execution whose previous lease expired, receives `execute` and a new attempt. A duplicate observed while another attempt owns a live lease receives `active_attempt` and is delayed with NAK until that lease can be reclaimed; it is never permanently ACKed. `waiting_feedback` receives its own admission and ACKs because Xarta durably owns the asynchronous continuation. `resolved` and `failed` receive `duplicate_terminal` and ACK without invoking the handler. Every redelivery is also checked against the complete immutable task identity, including node specification and trigger provenance; an execution-ID collision is a permanent integrity error.

Runtime terminal outcomes such as `execution_failed`, `retry_exhausted`, and a `TemporaryError` semantic outcome are execution events, not artificial tracked operations. Tracked consumers transactionally deduplicate the outcome by execution ID, mark their own execution terminal, and publish deterministic successors directly. Existing adapter bindings remain intact.

Peppol is a production tracked capability. A `PeppolNode` says only: submit this UBL document through Peppol. The deployed service configuration selects the adapter, and the UBL sender selects a provider account. e-invoice.be uses staged DRAFT creation and `/send`, checkpointing its account-scoped provider document identity between side effects and reconciling provider state. Recommand submits Xarta's inspected XML directly; because it documents no send idempotency or unique recovery key, an ambiguous response becomes `outcome_uncertain` without blind retry, while a definitive response is checkpointed and replayed locally. Both adapters normalize provider observations through the same reducer while Xarta owns durable tracking, callbacks, and semantic routing.

The built-in SMTP adapter is synchronous and creates no PostgreSQL workflow state. A lost response during `DATA` resolves directly through `outcome_uncertain`; explicit SMTP refusals remain retryable or semantic recipient outcomes. Successful SMTP submission resolves as `accepted`/`all_accepted`, never as delivered.

Ordinary SMTP cannot guarantee exactly-once external delivery. Xarta assigns the same deterministic `Nats-Msg-Id` to every replay of a node execution, but JetStream suppresses duplicate publications only within the stream's configured duplicate window. A redelivery after that window, or a successful SMTP submission followed by failure to publish its successors or persist the request ACK, can submit the message again. SMTP has no provider idempotency key or reconciliation API with which Xarta could prove that the first submission succeeded. Workflows requiring stronger external-delivery guarantees must use a tracked, idempotent provider such as Resend.

Resend is the first feedback-capable email adapter. It uses the tracked operation ID as a provider idempotency key, checkpoints the provider email ID, authenticates Svix callbacks, and normalizes callbacks and polling through one monotonic multi-recipient reducer. Delivery aggregates can emit while the operation remains open for a bounded complaint window. Provider event names remain evidence; applications route on provider-neutral email outcomes.

The generic email worker executes an adapter-supplied `EmailSubmissionPlan`. The plan
declares initial tracked state, provider-account correlation scope, the durable checkpoint
required before the side effect, and any safe idempotency deadline. SMTP supplies an
explicit uncertainty boundary; Resend supplies checkpointed idempotency. Adding another
provider does not add provider-name or provider-configuration branches to the worker.

SFTP destinations are logical names in the DAG. Kubernetes mounts a Secret containing the destination registry, host-key material, and authentication key. Tracking rows persist only destination, adapter, and revision. Registry entries may retain several revisions while selecting one `current_revision`; retries resolve the pinned revision, so rotating a destination never switches an active operation. Uploads use an operation-specific staging file and a final rename. A lost rename response resolves conservatively through `outcome_uncertain` rather than uploading the document again.

The Local Postal Adapter is specified as tracked because printing, operator scans,
physical handover, and registered-carrier observations continue after admission. Its
ambiguous side effects are physical: printer submission, handover, and registered-item
announcement cannot be made exactly once by deterministic task identity. Durable event
keys, generations, immutable package/source identities, and carrier reconciliation must
prevent blind repetition. Ordinary mail can resolve at operator-confirmed handover and
must never emit delivery merely because no further feedback exists. The protocol and
domain primitives are present, but the tracked adapter, persistence, APIs, and carrier
runtime are not yet wired; see `docs/postal-local-adapter.md`.

Email, SFTP, archive outbound, and search service configuration builders require every destination to declare `current_revision` and a non-empty `revisions` object. Every retained revision is validated at startup because an active operation may still be pinned to it. Direct `DestinationRegistry` construction keeps the historical content-hash fallback for local development and tests, but production service configuration must not rely on that compatibility mode.

## Adapter Limitation Documentation

Every adapter must document its execution mode, idempotency scope and expiry, ambiguous side-effect boundary, duplicate-delivery behavior, retry safety, and available callback or reconciliation mechanism. Deterministic Xarta task identities must not be described as exactly-once external side effects unless the provider contract actually supplies that guarantee.

- [ ] TODO: audit every existing adapter and add a dedicated limitations section covering these guarantees and failure boundaries.
