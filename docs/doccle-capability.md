# Doccle capability

## Architecture and contract

Doccle is a server-selected sender capability. `doccle.json` names one
`current_sender`; each configured sender has retained adapter revisions for endpoint
and credential rotation. The sender selection, adapter, endpoint, provider sender
identity, credentials, and document-type mapping are owned by the service. A sender
selector is deliberately absent from both Doccle DAG nodes and receiver control-plane
requests, so callers cannot override provider identity or credentials. The generic
tracking persistence calls this pinned value a destination binding, but Doccle domain
code and operator configuration call it a sender.

A **receiver** is Xarta's stable, destination-scoped routing identity. It has an
internal UUID, an immutable external receiver ID used with Doccle, a provisioning
state, and the latest known link state. It is not a Doccle **user account**. The
user separately links or unlinks their Doccle account to the provisioned receiver;
Xarta neither creates nor owns that account.

The sender REST exchange is synchronous:

- `PUT /senders/{sender_name}/receivers/{receiver_id}` sends the official
  `createUpdateReceiver` XML request.
- `POST /senders/{sender_name}/receivers/{receiver_id}/documents/{document_id}`
  sends the official `putDocument` XML request.
- Both use the official Atos archiving XML namespaces and Bearer authorization.
  Receiver success is HTTP 200/201 with no required body; document success returns
  `documentUri`. A successful response completes that exchange immediately. Doccle does
  not use `TrackedOperation`, and a Doccle node does not enter
  `WAITING_FEEDBACK`.
- `stored` means that Doccle accepted/stored the document. It does **not** prove
  delivery, user linking, user notification, viewing, or download.

The receiver selector is exactly one of:

- **subject mode**: `{"subject": {...}}` resolves a provisioned receiver in the
  server-selected destination.
- **explicit mode**: `{"id": "external-receiver-id"}` sends to that external
  receiver ID without a local subject lookup.

Subjects are stored as PostgreSQL `jsonb` and matched by exact `jsonb` equality
within a destination. Object key order is insignificant, but values, types,
nested structure, and the presence of additional keys are significant. For
example, `{"customer_id":"C-1"}` is not the same subject as
`{"customer_id":"C-1","tenant":"T-1"}`. Subjects must be non-empty JSON
objects. Do not put secrets or mutable profile data in a subject.

## Receiver and link lifecycle

The authenticated control plane provides:

- `PUT /api/v1/doccle/receivers/ensure` with exactly `subject` and `profile`.
  It inserts or loads the destination/subject identity and synchronously calls
  receiver PUT when provisioning is required.
- `POST /api/v1/doccle/receivers/resolve` with exactly `subject`.
- `GET /api/v1/doccle/receivers/{receiver_id}`.

Receiver states are `pending`, `provisioning`, `provisioned`, `uncertain`, and
`failed`. Provisioning has a durable lease token and expiry. A process failure can
therefore be reclaimed using the exact same external Receiver ID, while a stale
claimant cannot overwrite the newer owner. A successful PUT establishes
`provisioned` while `linked` remains
unknown; it does not establish an account link. Authenticated
`RECEIVER_LINK_UNLINK` callbacks set
the latest `linked` boolean. Every authenticated callback receipt is retained as
`jsonb` with a database receipt order and an optional canonical payload fingerprint.
The fingerprint is audit evidence, not event identity: `true → false → true` applies
all three receipts. Projection updates are ordered by the database receipt order and
are applied only when the sender
maps unambiguously to one current server destination and the external receiver
exists. Link state is lifecycle information, not document-delivery confirmation.

The control-plane JSON shapes are exact. For example:

```json
{
  "subject": {"customer_id": "C-1", "tenant": "T-1"},
  "profile": {
    "label": "Synthetic customer C-1",
    "first_name": "Synthetic",
    "last_name": "Customer",
    "email": "c-1@example.invalid",
    "language": "en"
  }
}
```

The profile permits only `label`, `first_name`, `last_name`, `email`, and
`language`; only `label` is required. `ensure()` means “ensure this exact subject has
a provisioned Receiver identity.” The profile is provisioning input only when
provisioning is required. Calling `ensure()` for an already provisioned subject does
not silently update its provider profile; a future profile update requires an
explicit control-plane operation.

## Submission safety

Before POST, Xarta durably pins the server-selected destination, adapter name,
destination configuration revision, local receiver UUID when applicable,
external receiver ID, provider document ID, source document UUID, SHA-256 source
digest, and selector mode. The provider document ID is the node execution UUID.
Those identity fields are immutable. A retry re-resolves the pinned destination
revision and verifies the source UUID and digest before crossing the POST
boundary.

Failures known to occur before transmission may return to `prepared` and retry.
A timeout or client failure after request construction is ambiguous: the provider
may have stored the request even though Xarta did not receive its response. Such
submissions become `uncertain` and emit `outcome_uncertain`; they are not blindly
replayed. Likewise, recovery of a persisted `submitting` row treats the POST
boundary as crossed and does not replay it.

The current OpenAPI states that a document with the same `documentId` is
overwritten, but it does not promise duplicate suppression or no additional provider
version for identical content. Do not infer idempotency from stable resource identity
or receiver PUT semantics, and do not manually replay uncertain document POSTs.

## Server configuration

The Doccle blueprint uses these service environment variables:

- `NATS_SERVERS`
- `TMP_STORAGE`
- `POSTGRESQL_USER`
- `POSTGRESQL_PASSWORD`
- `POSTGRESQL_DATABASE`
- `POSTGRESQL_HOST`
- `DOCCLE_CONFIGURATIONS_CONFIG_PATH`: path to the sender JSON file.
- `DOCCLE_CONTROL_PLANE_TOKEN`: bearer token for receiver control endpoints.
- `DOCCLE_CALLBACKS_ENABLED`: optional `true`/`false`, default `false`.
- `DOCCLE_CALLBACK_CERTIFICATE_FINGERPRINTS`: required only when callbacks are
  enabled; comma-separated SHA-256 certificate fingerprints, with or without
  colons, for callback mutual-TLS authentication.

The sender file has this exact revisioned shape:

```json
{
  "current_sender": "doccle-acc",
  "senders": {
    "doccle-acc": {
      "adapter": "doccle-sender-rest",
        "current_revision": "acc-v1",
        "revisions": {
          "acc-v1": {
            "sender_name": "ACC_SENDER_NAME",
            "endpoint": "https://ACC_ENDPOINT",
            "timeout": 10,
            "credentials": {
              "token_url": "DOCCLE_OIDC_TOKEN_URL",
              "client_id": "DOCCLE_CLIENT_ID",
              "client_secret": "DOCCLE_CLIENT_SECRET"
            },
            "document_types": {"synthetic": "ACC_DOCUMENT_TYPE"},
            "max_response_bytes": 1000000
        }
      }
    }
  }
}
```

`receiver_path` and `document_path` are optional; when supplied they must be
absolute paths containing both `{sender_name}` and `{receiver_id}`. The document
path must additionally contain `{document_id}`. Credentials must not be embedded
in the HTTPS endpoint.

The core Helm chart keeps Doccle disabled by default. Its exact values are
`services.doccle.enabled`, `replicas`, `existingSecret`,
`settings.config`, `settings.nats.workers`,
`settings.callbacks.enabled`, `settings.callbacks.certificateFingerprints`,
`ingress.enabled`, `callbackIngress.enabled`, and
`callbackIngress.clientCertificateAuthoritySecret`.
Callbacks and their ingress are disabled by default. When `existingSecret` is
set, that Secret must contain these
exact data keys:

- `doccle.json`: `current_sender` plus revisioned `senders`, including each
  sender's OAuth client credentials. The adapter obtains and caches short-lived
  Bearer tokens; access tokens are not durable configuration.
- `control-plane-token`: the bearer token.
- `callback-certificate-fingerprints`: required only when callbacks are enabled.

When `existingSecret` is absent, the chart creates `<release>-core-doccle` from
`settings.config`, `settings.controlPlaneToken`, and, only when callbacks are
enabled, `settings.callbacks.certificateFingerprints`; avoid this mode if Helm
values are not stored with secret-grade controls. The deployment mounts
`doccle.json` read-only at `/mnt/config/doccle.json`, sets the applicable
`DOCCLE_*` variables listed above, and also wires NATS, temporary storage,
PostgreSQL, document-source
service endpoints, and timeouts. `k8s/helm/doccle-acc-values.example.yaml` is the
ACC `existingSecret` example.

## ACC contract tests

The tests use the production `DoccleSenderRESTAdapter` and synthetic UUID-based
data. They create persistent provider-side receivers and documents and perform no
cleanup because no delete contract exists. Use a dedicated ACC sender, obtain
approval for side effects, confirm response status names and a harmless document
type with Doccle, and run from a controlled workstation or CI job whose output is
access-restricted. Never put production credentials or personal data in these
variables.

Required test environment:

- `DOCCLE_ACC_TESTS=1`
- `DOCCLE_ACC_ENDPOINT`
- `DOCCLE_ACC_SENDER_NAME`
- `DOCCLE_ACC_USERNAME`
- `DOCCLE_ACC_PASSWORD`

Optional contract configuration:

- `DOCCLE_ACC_PROVIDER_DOCUMENT_TYPE` (default `OTHER`)
- `DOCCLE_ACC_SUCCESS_STATUSES` (comma-separated, default `SUCCESS`)
- `DOCCLE_ACC_REJECTION_STATUSES` (comma-separated, default
  `RECEIVER_UNKNOWN`)
- `DOCCLE_ACC_TIMEOUT_SECONDS` (default `10`)

Duplicate POSTs are separately protected by `DOCCLE_ACC_ALLOW_REPLAY_TESTS=1`.
They record categories and provider status strings but intentionally do not claim
that either same-content or changed-content replay is idempotent. Authentication
failure and forced-timeout probes are separately protected by
`DOCCLE_ACC_ALLOW_AUTH_FAILURE_TESTS=1` and
`DOCCLE_ACC_ALLOW_TIMEOUT_TESTS=1`; both can trigger provider security telemetry,
and the timeout probe may still create a receiver.

Safe procedure:

1. Export the required variables from an approved secret manager without shell
   tracing and verify that the endpoint is ACC HTTPS, not production.
2. Run the base suite first and review its synthetic artifacts with the Doccle
   ACC operator.
3. Obtain explicit provider approval before enabling authentication, timeout, or
   replay probes.
4. Retain the pytest/JUnit observation properties and correlate timestamps and
   synthetic IDs with provider logs. Do not retain credentials or XML bodies.
5. Have two operators review any `uncertain`, protocol-error, or replay result;
   never manually resend an ambiguous document as remediation.

Separately runnable base command:

```bash
pytest -m doccle_acc tests/services/test_doccle_acc.py
```

Replay-only command after explicit approval:

```bash
DOCCLE_ACC_ALLOW_REPLAY_TESTS=1 pytest -m doccle_acc tests/services/test_doccle_acc.py -k replay
```

## Security and operator audit

The adapter requires HTTPS, uses Basic authentication, validates response size,
rejects XML DTDs/entities, disables redirects, and URL-escapes sender and receiver
IDs. Control endpoints use a constant-time bearer-token comparison and bounded
JSON bodies. Callbacks require an allow-listed peer certificate fingerprint and
bounded JSON. When Kubernetes ingress terminates TLS, NGINX must verify the client
certificate against the configured Doccle CA and replace
`ssl-client-cert` with the verified PEM certificate; Xarta then verifies
its configured SHA-256 fingerprint. Rotate ACC and production credentials
independently, restrict the
destination file to the service identity, treat control tokens and fingerprints
as secrets/configuration with change control, and redact request/response bodies
from logs.

Operators audit `doccle_receivers`, `doccle_receiver_callbacks`, and
`doccle_submissions`, plus node execution records and provider logs. Record the
destination and pinned revision, external receiver ID, document ID, source digest,
state transitions, semantic outcome, provider reference/status when available,
callback identity/application state, actor, timestamp, and decision. Do not place
credentials, document content, or unnecessary personal data in audit tickets.
Resolving `uncertain` submissions or changing a response-status mapping requires
provider evidence and an auditable operator decision.
