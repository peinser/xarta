# NATS Failure Classification And Dead Letters

Xarta classifies JetStream handler failures by whether repeating the same message can
reasonably succeed. This classification controls retries, workflow outcomes, and the
dead-letter record used for operational diagnosis.

## Failure Classes

| Failure | Handler representation | Delivery behavior | Dead letter |
|---|---|---|---|
| Expected domain result | `CapabilityResult` or `OutcomeEmission` | Publish exact outcome and successors, then ACK | No |
| Retryable technical failure | `TemporaryError` | NAK with the requested delay | After retry exhaustion |
| Explicit permanent failure | `PermanentError` | Publish `execution_failed` when a task can be parsed, publish dead letter, then terminate | Immediately |
| Malformed request | Request parsing failure | Publish dead letter, then terminate | Immediately |
| Poison identity conflict | `PermanentError` with `poison` classification | Preserve the accepted execution, publish dead letter, then terminate | Immediately |
| Unexpected exception | Any other exception | Retry with a one-second delay | After three deliveries |

Expected business outcomes are not infrastructure failures. For example, an archive
version conflict, a rejected destination, or a provider's declared terminal outcome must
be represented by the capability's finite outcome contract and must not be dead-lettered.

`TemporaryError` defaults to the consumer's `NATS_MAX_DELIVER` limit. A handler may set a
smaller `max_deliver` when its retry contract requires one. The runtime caps every
exception-specific value at `NATS_MAX_DELIVER`. Unexpected exceptions use three
deliveries so intermittent defects receive a bounded retry without hiding persistent
programming or contract failures.

The default delivery backoff is exponential: `delay * 2^(delivery - 1)`, capped at five
minutes. A capability can select another multiplier and cap. Capabilities such as
`wait-for` that choose a complete delay sequence themselves set `backoff_multiplier=1` so
the runtime does not apply a second exponential policy.

Use `PermanentError` only when retrying the exact message cannot succeed. External calls
with an ambiguous side-effect boundary are not permanent failures: they require the
adapter's documented reconciliation or tracked-operation path.

`TemporaryError(auto_replay=True)` is a separate, explicit adapter guarantee that a
request remains safe to repeat after the normal JetStream retry window. It is not inferred
from `retryable`. The adapter must have a reviewed idempotency boundary that remains valid
for the entire periodic retry interval. The local archive adapter opts in because every
archive write carries a deterministic idempotency key and immutable document/version
identity. Provider calls with ambiguous side effects must not opt in.

## Ordering And Acknowledgement

For a parsed request that reaches a terminal technical failure, Xarta:

1. Publishes the dead-letter record with a deterministic message ID derived from the
   original message ID.
2. Applies the deterministic `execution_failed` or `retry_exhausted` outcome and
   publishes deterministic successors.
3. Terminates the original delivery only after both operations succeed.

Publishing the dead letter first prevents a tracked terminal outcome from being committed
without its diagnostic record. Xarta retains ownership and retries dead-letter publication
with progress heartbeats. If the process stops, JetStream can redeliver the original
message. Outcome, successor, and dead-letter identities must therefore remain
deterministic. JetStream deduplication is bounded by the stream's duplicate window and is
not an exactly-once side-effect guarantee.

Malformed messages cannot produce a workflow outcome because their task identity is not
trusted. They are dead-lettered directly and terminated only after publication succeeds.

## Dead-Letter Subjects

Each durable consumer publishes to:

```text
<stream-lowercase>.dead-letter.<durable>
```

Examples:

```text
requests.dead-letter.generate
requests.dead-letter.signature
jobs.dead-letter.setup_nats
```

The stream must include the dead-letter subject in its configured subject hierarchy. A
dead-letter consumer should filter by the structured headers rather than introducing a
new subject for every failure class.

## Headers

Every dead-letter message preserves the original headers and adds:

| Header | Meaning |
|---|---|
| `Nats-Msg-Id` | Original message ID with the deterministic `:dead-letter` suffix |
| `X-Xarta-Dead-Letter-Version` | Envelope schema version, currently `1` |
| `X-Xarta-Failure-Class` | `malformed`, `poison`, `permanent`, `retry_exhausted`, or `unexpected_retry_exhausted` |
| `X-Xarta-Error-Code` | Stable machine-readable failure code |
| `X-Xarta-Retryable` | Whether the original failure was retryable |
| `X-Xarta-Auto-Replay` | Whether the adapter explicitly permits periodic replay |
| `X-Xarta-Delivery-Count` | JetStream delivery count at terminal handling |
| `X-Xarta-Periodic-Replay-Count` | Number of completed periodic replay handoffs |
| `X-Xarta-Periodic-Replay-Limit` | Limit pinned by the first periodic replay |
| `X-Xarta-Periodic-Retry-Final` | Whether failure of this replay emits the terminal outcome |

Headers are intended for filtering and alert routing. The JSON envelope is authoritative.

## Envelope Version 1

```json
{
  "schema_version": 1,
  "classification": "unexpected_retry_exhausted",
  "error_code": "unexpected_handler_error",
  "error": "Retryable NATS delivery exhausted",
  "original_subject": "requests.<flow>.tasks.<execution>.generate",
  "original_message_id": "<message-id>",
  "stream_sequence": 42,
  "consumer": "generate",
  "delivery_count": 3,
  "first_seen_at": "2026-08-30T12:00:00Z",
  "failed_at": "2026-08-30T12:00:03Z",
  "flow_id": "<flow-id>",
  "node_id": "<node-id>",
  "node_execution_id": "<node-execution-id>",
  "outcome": {
    "outcome": "execution_failed",
    "details": null
  }
}
```

Fields that cannot be recovered from a malformed message are `null`. The envelope does
not duplicate the original payload. Flow specifications can contain personal data,
document data, provider details, or credentials, and copying them into an operational
failure stream would increase exposure. Operators can use the original stream sequence
and subject while the source stream retention window remains active.

Dead-letter publication also allowlists headers. Message identity, flow correlation, and
periodic-replay control headers are retained; authorization, payment, provider, and other
arbitrary source headers are not copied into the operational record.

Error messages must be concise and must not contain credentials, private keys, document
contents, payment signatures, or provider secrets. Full exception traces belong in
application logs correlated by flow, node, and execution IDs.

## Periodic Replay

The `replay-dead-letters` CronJob runs at 00:00, 10:00, and 20:00 by default. It uses the
durable `dead-letter-replay` consumer and processes a bounded batch. A record is eligible
only when all of the following are true:

- `X-Xarta-Failure-Class` is `retry_exhausted`;
- `X-Xarta-Retryable` is `true`;
- `X-Xarta-Auto-Replay` is `true`.

The job retrieves the original source-stream message by `stream_sequence`, republishes its
original subject, data, and headers, and derives a deterministic replay message ID by
appending `:periodic-replay:<attempt>` to the original ID. This avoids suppression when a
replay happens inside JetStream's duplicate window without sacrificing deterministic
identity. The job also adds `X-Xarta-Periodic-Replay-Count`. The default maximum is three
periodic attempts. The first replay pins that limit in
`X-Xarta-Periodic-Replay-Limit`, so a later Helm configuration change cannot alter an
in-flight retry policy. The last attempt carries `X-Xarta-Periodic-Retry-Final: true`.

An auto-replayable failure does not emit `retry_exhausted` when the normal delivery budget
is first exhausted. Emitting a terminal failure branch before a later successful replay
would produce contradictory workflow outcomes. If the final periodic attempt also
exhausts normal delivery, Xarta publishes the terminal outcome and no further automatic
replay occurs.

The Helm controls are:

```yaml
jobs:
  replayDeadLetters:
    enabled: true
    schedule: "0 0,10,20 * * *"
    batchSize: 100
    maxAttempts: 3
```

Changing the schedule or maximum extends the adapter's required idempotency period and
must be reviewed against every capability that sets `auto_replay=True`.

The replay publish and dead-letter acknowledgement are separate JetStream operations. The
job uses a synchronous acknowledgement, but a process failure after publish and before
ACK can still cause the same periodic attempt to be handed off again after the stream's
duplicate window. This is why periodic replay requires the stronger `auto_replay`
idempotency guarantee; deterministic message identity alone is not an exactly-once
guarantee.

## Operations And Manual Replay

Alert on new dead-letter messages and group them by consumer, classification, and error
code. Before replaying a message:

1. Inspect the correlated application log and workflow outcome.
2. Correct the configuration, contract, or code defect.
3. Confirm that replay is safe under the capability adapter's idempotency and ambiguous
   side-effect contract.
4. Retrieve the original message from the source stream using its stream sequence.
5. Republish with the original subject and deterministic message identity under an
   explicit operator action.

Never automatically replay `poison`, `malformed`, `permanent`, or
`unexpected_retry_exhausted` messages, or operations with an ambiguous external side
effect. For those classes, a dead-letter record is diagnostic evidence, not permission to
repeat an external operation.
