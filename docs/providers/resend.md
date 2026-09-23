# Resend Email Adapter

Last verified against Resend and Svix documentation: 2026-08-29

## Purpose

Resend is Xarta's first feedback-capable asynchronous email adapter. Applications submit
an ordinary provider-neutral `EmailNode`. The normalized sender address selects a
server-owned email binding; applications never submit a Resend API key, account ID,
webhook secret, provider email ID, or provider event name. The legacy `destination` field
remains accepted for existing flows but should be omitted from new contracts.

The adapter demonstrates Xarta's tracked-capability model:

```text
EmailNode
  -> TrackedOperation
  -> POST /emails with Idempotency-Key
  -> durable provider email ID
  -> accepted outcomes
  -> authenticated webhook or reconciliation observation
  -> provider-neutral email update
  -> shared reducer
  -> semantic OutcomeEvent and DAG routing
```

## Semantics

One `EmailNode` creates one Resend message. `to`, `cc`, and `bcc` are not split into
separate provider messages because doing so would change visible headers, reply-all,
threading, and message identity.

Xarta exposes these finite recipient outcomes:

- `accepted`: Resend accepted responsibility for sending; not mailbox delivery.
- `delivery_delayed`: Resend reported a temporary delivery delay; nonterminal.
- `delivered`: the recipient mail server accepted the message; not read or inbox proof.
- `mailbox_full`: permanent failure evidence specifically identifies enhanced status
  `4.2.2` or `5.2.2`.
- `recipient_unknown`: permanent failure evidence identifies enhanced status `5.1.1`.
- `message_rejected`: permanent failure, suppression, or provider failure without a more
  precise finite semantic classification.
- `complained`: a delivered recipient subsequently reported the message as spam.

Aggregate outcomes remain `all_accepted`, `partially_accepted`, `all_delivered`,
`partially_delivered`, and `all_failed`.

Provider event names and error strings remain bounded evidence in `OutcomeEvent.details`.
Applications route only on finite outcomes.

## Destination Configuration

Mount one generic email destination registry through `EMAIL_CONFIGURATIONS_CONFIG_PATH`:

```json
{
  "payroll@example.com": {
    "kind": "email",
    "adapter": "resend-rest",
    "current_revision": "2026-08-29.1",
    "revisions": {
      "2026-08-29.1": {
        "base_url": "https://api.resend.com",
        "api_key": "<RESEND_API_KEY>",
        "webhook_secret": "<RESEND_WHSEC_SECRET>",
        "account_id": "resend-production-account",
        "timeout": 30,
        "feedback_retention_seconds": 86400
      }
    }
  }
}
```

The top-level key is the normalized sender mailbox. For example,
`Payroll <payroll@example.com>` resolves `payroll@example.com`. `account_id` is Xarta's
immutable provider-account namespace and does not come from the DAG. Provider references
are correlated by:

```text
adapter + provider account ID + Resend email ID
```

Every active revision must remain mounted while an operation owns it. Configuration is
validated locally at startup; Xarta makes no Resend HTTP calls merely to boot.

In Kubernetes, prefer a content-addressed externally managed Secret and set
`services.email.existingSecret`. The Secret must contain `email.json`. If a fixed Secret
name is unavoidable, update `services.email.existingSecretChecksum` on every rotation so
the Deployment rolls. The whole Secret directory is mounted rather than a `subPath`, but
the service intentionally reloads configuration only at process startup.

## Domain And Sender Setup

1. Create separate Resend accounts or clearly separated API keys for development and
   production.
2. Add and verify the sending domain in Resend.
3. Publish the exact SPF and DKIM records supplied by Resend.
4. Wait for Resend domain verification before production cutover.
5. Create a least-privilege sending API key and store it only in secret management.
6. Configure an application sender belonging to the verified domain.
7. Confirm the sender-address binding selects the intended account and environment.

Do not put API keys in a flow profile, `EmailNode`, ConfigMap, log, OutcomeEvent, or
request payload.

## Idempotent Submission

Xarta uses the immutable `TrackedOperation.id` as Resend's `Idempotency-Key`. Before the
first HTTP request, Xarta durably checkpoints `submission_started_at` and
`idempotency_valid_until`.

Resend retains idempotency results for 24 hours. During that window, timeout, connection
loss, `429`, temporary `5xx`, and `concurrent_idempotent_requests` can retry the exact
same payload and key. Resend returns the original result instead of creating another
message.

Xarta fingerprints the canonical Resend request before the first call and persists that
fingerprint. A retry whose body, recipients, rendered HTML, or attachments changed is
stopped as uncertain rather than reusing the key with another payload. Retries stop at
least one configured HTTP timeout plus 60 seconds before Xarta's conservative 24-hour
deadline, avoiding a request that crosses the provider retention boundary in flight.

If the provider ID remains unknown after the 24-hour window, Xarta does not submit the
same key again. It resolves conservatively through `outcome_uncertain`; operators must
not manually create another message without deterministic evidence.

`invalid_idempotent_request` means one key was reused with a different body and is a
configuration/integrity failure, not a safe transient retry.

Before submission, Xarta serializes the exact sorted-key JSON request and applies a
conservative 40 MiB limit to those encoded bytes. The calculation includes base64
expansion across every attachment, plain text, HTML, addressing fields, attachment
metadata, and JSON encoding. Validation errors report both estimated and allowed bytes.
This is intentionally stricter than checking raw attachment bytes alone.

## Webhook Setup

Create one Resend webhook per sender binding/revision for:

```text
https://<XARTA_HOST>/api/v1/email/callbacks/resend/payroll@example.com
```

Subscribe to:

```text
email.sent
email.delivery_delayed
email.delivered
email.bounced
email.failed
email.suppressed
email.complained
```

Do not subscribe this endpoint to opens or clicks; Xarta does not fabricate routing
semantics for engagement tracking.

Resend signs webhooks through Svix. Xarta verifies the exact raw body using
`svix-id`, `svix-timestamp`, and every `v1` candidate in `svix-signature`. It decodes the
`whsec_` secret, computes HMAC-SHA256 over:

```text
svix-id + "." + svix-timestamp + "." + raw-body
```

and compares signatures in constant time. The timestamp tolerance is five minutes.
`svix-id` is the durable callback deduplication identity. JSON parsing and operation
mutation happen only after authentication.

The callback path's sender binding is an untrusted routing hint. Successful authentication
must use a secret from that binding's retained revisions, and the correlated operation
must own that exact revision and account ID.

## Delivery Lifecycle And Complaint Window

Submission acceptance emits recipient `accepted` outcomes and `all_accepted`, but leaves
the operation open. Delivery callbacks update recipient state monotonically:

```text
accepted -> delivery_delayed -> delivered -> complained
accepted -> delivery_delayed -> permanent failure
```

Complaints imply delivery. If a complaint arrives before the delivered callback, Xarta
emits `delivered` and `complained` in one atomic reduction.

After all recipients have a terminal delivery disposition, Xarta emits the delivery
aggregate and starts `feedback_retention_seconds`. The operation remains open during this
bounded complaint window, allowing a later complaint to remain an application-routable
fact. The reconciler closes the operation only after the delivery aggregate exists and
the post-settlement window has elapsed.

Conflicting terminal evidence, such as a permanent bounce after delivery, is retained in
bounded operation state for operator diagnosis. Arrival order does not silently reverse a
terminal fact.

## Reconciliation

Webhooks reduce latency; they are not the durability mechanism. Replicas claim bounded
leased batches of open Resend operations and call:

```text
GET /emails/{provider_email_id}
```

Provider retrieval proves submission acceptance. For a single-recipient message,
`last_event` is normalized through the same reducer used by callbacks. Resend's retrieve
API exposes only one message-level `last_event`, so Xarta deliberately does not apply it
to every recipient of a multi-recipient message: doing so could fabricate `all_delivered`
or `all_failed`. Multi-recipient delivery convergence therefore requires recipient-aware
webhooks; reconciliation still restores acceptance and closes already-settled complaint
windows.

Claims use `FOR UPDATE SKIP LOCKED`, bounded concurrency, and exponential backoff, so
replicas do not all poll the same operation.

Environment controls:

```text
EMAIL_RECONCILIATION_BATCH_SIZE
EMAIL_RECONCILIATION_CONCURRENCY
EMAIL_RECONCILIATION_INTERVAL_SECONDS
EMAIL_RECONCILIATION_LEASE_SECONDS
EMAIL_RECONCILIATION_MAX_BACKOFF_SECONDS
```

## Development Test Recipients

Resend provides deterministic recipients:

```text
delivered@resend.dev
bounced@resend.dev
complained@resend.dev
suppressed@resend.dev
```

Labels isolate test runs:

```text
delivered+xarta-<test-id>@resend.dev
bounced+xarta-<test-id>@resend.dev
complained+xarta-<test-id>@resend.dev
suppressed+xarta-<test-id>@resend.dev
```

For each contract test, use a unique flow ID, operation ID, and label. Assert the send
response, authenticated callback, operation state, finite recipient outcome, aggregate,
and child activation. Never call these tests a real mailbox or end-to-end human delivery
test.

The free tier is useful for development but quotas are external provider policy. Monitor
usage and do not make correctness depend on a particular free-tier allowance.

### Opt-In Live Contract Suite

The deterministic unit suite never requires credentials. Run the live provider boundary
separately:

```bash
export RESEND_CONTRACT_API_KEY='...'
export RESEND_CONTRACT_WEBHOOK_SECRET='whsec_...'
export RESEND_CONTRACT_ACCOUNT_ID='resend-test-account'
export RESEND_CONTRACT_FROM='Xarta Contract <contract@verified.example>'
pytest -m resend_contract tests/services/test_resend_contract.py
```

The default live test submits one uniquely labelled `delivered@resend.dev` message,
repeats the exact idempotent request, verifies the same provider email ID, polls with a
bounded 90-second deadline, and verifies retrieval/message-ID correlation. Set
`RESEND_CONTRACT_ALL_RECIPIENTS=true` to add bounced, complained, and suppressed cases;
these consume provider quota and may take longer.

Resend does not provide a reliable pull API for obtaining the exact HTTP webhook body and
Svix headers delivered to an arbitrary test runner. Raw webhook verification and
normalization therefore remain pinned by deterministic local contract fixtures. Full
live callback correlation requires a deployed callback endpoint and is part of the
environment smoke test, not normal CI. The suite never prints credentials.

## Observability

Operators can inspect:

```text
GET /api/v1/email/operations/{tracked_operation_id}
```

The response contains pinned destination/adapter/revision/account identity, provider email
ID, SMTP `Message-ID` when Resend has exposed it, lifecycle, normalized recipient state,
bounded contradictions, and immutable outcomes. SMTP `Message-ID` is correlation metadata
only; it never replaces Xarta execution identity, tracked-operation identity, the Resend
email ID, or the idempotency key.

The dedicated callback Ingress contains only `/api[/v1]/email/callbacks/resend/...` paths.
It does not expose `/operations`. Operational status remains an internal/platform-authenticated
API. Xarta's broader platform authentication and tenancy work remains a separate concern;
Svix authentication is provider callback authentication, not user authorization.

Recommended metrics and alerts:

- send request totals and latency by destination/revision/status class;
- idempotent retries and `409` reason;
- operations approaching or exceeding idempotency expiry without provider ID;
- callback authentication failures by destination;
- callback age and duplicate rate;
- open operations older than expected delivery time;
- reconciliation claim, success, failure, and backoff counts;
- delivered, delayed, bounced, suppressed, failed, and complained rates;
- contradiction count;
- provider `429`, quota, authentication, and `5xx` rates;
- operations past feedback-window closure that remain open.

Never label `accepted` as delivered, or `delivered` as read, approved, or business
accepted.

## Incident Procedures

### API timeout or connection reset

Verify the operation's `submission_started_at` and idempotency expiry. Inside 24 hours,
allow Xarta to retry the same key. Do not submit manually. After expiry with no provider
ID, treat the outcome as uncertain and investigate Resend activity before any replacement.

### Callback authentication failures

Check the exact destination URL, retained revision, `whsec_` secret, proxy body handling,
clock skew, and whether a proxy modified the body. Do not disable signature checks.

### Lost or delayed webhooks

Check callback ingress and Resend webhook history. Reconciliation should converge from
`last_event`; monitor its claims and backoff. A callback replay is safe because `svix-id`
is deduplicated.

### High bounce or suppression rate

Stop affected traffic if necessary. Inspect finite outcomes and bounded diagnostics,
sender/domain health, list provenance, and Resend suppression state. Do not blindly retry
permanent failures.

### Credentials revoked

`401`/`403` is not transient provider downtime. Add a new retained revision with the new
API key and webhook secret. Existing operations must keep credentials capable of callback
authentication and reconciliation until they drain.

### Contradictory terminal evidence

Inspect Resend's event history and retrieved email, preserve all evidence, and avoid
manually overriding state based on callback arrival order.

## Rotation

1. Create a new API key and webhook endpoint/secret.
2. Add a new immutable destination revision; do not rewrite the old revision.
3. Deploy and verify configuration locally.
4. Send all four deterministic test-recipient cases through the new revision.
5. Switch `current_revision` for new operations.
6. Retain the old API key and webhook secret while old operations remain open.
7. Remove the old revision only after delivery and complaint windows drain.

## Production Cutover Checklist

- Sending domain is verified with current SPF and DKIM records.
- Production sender belongs to the verified domain.
- API key and webhook secret are in secret management.
- Callback ingress is HTTPS, direct, bounded, and does not redirect.
- Svix signature and replay tests pass.
- Idempotency retry tests pass for timeout, `429`, `5xx`, and concurrent `409`.
- All four Resend test-recipient outcomes pass through Xarta routing.
- Reconciliation converges with webhook delivery disabled.
- Complaint-window closure is observed.
- Dashboards and alerts are active.
- Operators understand accepted, delivered, complained, and uncertain semantics.

## Authoritative References

- [Send Email API](https://resend.com/docs/api-reference/emails/send-email)
- [Idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys)
- [Webhook events](https://resend.com/docs/webhooks/event-types)
- [Webhook verification](https://resend.com/docs/webhooks/verify-webhooks-requests)
- [Webhook retries and replays](https://resend.com/docs/webhooks/retries-and-replays)
- [Retrieve Sent Email](https://resend.com/docs/api-reference/emails/retrieve-email)
- [Svix manual verification](https://docs.svix.com/receiving/verifying-payloads/how-manual)
