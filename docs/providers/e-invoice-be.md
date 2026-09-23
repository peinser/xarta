# e-invoice.be

Last verified against provider documentation: 2026-08-28

## Contract

e-invoice.be is Xarta's first Peppol adapter. A `PeppolNode` contains no provider
destination, account, API key, or environment. It says only: submit this UBL document
through Peppol.

The UBL supplier `EndpointID` selects a provider tenant through server-owned
configuration. The UBL remains authoritative for document kind, business document ID,
issue date, `CustomizationID`, `ProfileID`, sender, and receiver. Xarta always supplies
all four explicit sender/receiver Peppol routing parameters to `/send`.

The initial adapter supports UBL 2.1 Invoice and CreditNote documents using Peppol BIS
Billing 3 Profile 01. `SENT` maps to `delivery_confirmed` at the level exposed by this
provider. It does not mean viewed, approved, business-accepted, or paid.

## Configuration

One deployed Peppol service configuration owns current and retained revisions. Each
e-invoice.be tenant contains its provider credentials and one or more Peppol participant
IDs. The provider currently allows one Peppol ID per tenant, but Xarta does not encode
that provider limitation into its architecture.

```json
{
  "adapter": "e-invoice-be-rest",
  "current_revision": "v2",
  "revisions": {
    "v2": {
      "base_url": "https://api.e-invoice.be",
      "tenants": [
        {
          "tenant_id": "ten_123",
          "api_key": "<API_KEY_FROM_SECRET>",
          "webhook_secret": "<WEBHOOK_SECRET_FROM_SECRET>",
          "peppol_ids": [
            {"scheme": "0208", "identifier": "0123456789"}
          ]
        },
        {
          "tenant_id": "ten_456",
          "api_key": "<API_KEY_FROM_SECRET>",
          "webhook_secret": "<WEBHOOK_SECRET_FROM_SECRET>",
          "peppol_ids": [
            {"scheme": "0208", "identifier": "9876543210"}
          ]
        }
      ]
    }
  }
}
```

Set `PEPPOL_CONFIGURATIONS_CONFIG_PATH` to this read-only mounted file. Tenant IDs and
participant mappings must be unique. Keep an old revision while any active operation
owns it.

Startup validates configuration locally. Before the first provider read or side effect
for a `(configuration revision, tenant)`, Xarta calls `/api/me/`, verifies the configured
Peppol IDs, and caches success for that immutable revision. Concurrent first requests
share one validation. One tenant's failed identity check does not prevent unrelated
tenants from starting or sending.

If the UBL sender has no configured tenant, Xarta emits `execution_failed` with
`stage=validation` and `category=configuration_error` before provider validation,
participant lookup, DRAFT creation, or `/send`.

## Webhook Authentication

Register `document.sent` and `document.sent.failed` for:

```text
https://<XARTA_HOST>/api/v1/peppol/callbacks/e-invoice-be
```

The provider's current signing representation is the complete parsed event serialized
as UTF-8 using Python `json.dumps(payload, sort_keys=True)` semantics. This includes the
default separator spaces and ASCII escaping demonstrated by the provider's official
fixture. It is not the raw HTTP body and is not RFC 8785.

Xarta bounds and parses the body, then reads `tenant_id` and `data.document_id` only as
untrusted routing hints. It finds retained credentials for that tenant, canonicalizes
the complete event, computes HMAC-SHA256 with that tenant's webhook secret, and compares
the `sha256=...` value with `hmac.compare_digest`. No operation state changes before
authentication succeeds.

After authentication, correlation uses:

```text
adapter + provider tenant ID + provider document ID
```

The provider event `id` is the deduplication identity. Xarta does not route directly on
callback state; it re-reads `GET /api/documents/{id}` with the operation's pinned
revision and tenant, then sends the normalized observation through the shared reducer.

## Side-Effect Boundaries

Provider UBL validation, participant lookup, `/api/me/`, and document GET are read-only.
Transient transport, rate-limit, and server failures are retryable.

DRAFT creation and `/send` are separate non-idempotent boundaries:

```text
POST /api/documents/ubl
  -> persist DRAFT ID and commit
  -> POST /api/documents/{id}/send
```

An ambiguous creation response with no provider ID is terminal `outcome_uncertain`; Xarta
does not blindly create another DRAFT. An ambiguous send with a known ID remains
`UNCERTAIN` and `WAITING_FEEDBACK`. A DRAFT observation preserves uncertainty, TRANSIT
clears it and emits `submitted`, and SENT/FAILED resolve normally. Periodic leased
reconciliation provides convergence when callbacks are lost.

## Rotation

Create a new retained configuration revision for API-key or webhook-secret rotation.
Switch `current_revision` only for new operations. Existing operations keep their pinned
configuration revision and provider tenant identity for callbacks and reconciliation.
Remove old credentials only after their operations drain.

## Provider Links

- [Account identity](https://docs.e-invoice.be/api-reference/tenant/get-information-about-your-account)
- [Sending UBL documents](https://docs.e-invoice.be/guides/ubl-documents)
- [Send routing parameters](https://docs.e-invoice.be/api-reference/documents/send-document)
- [Webhooks and HMAC](https://docs.e-invoice.be/essentials/webhooks)
- [Admin API](https://docs.e-invoice.be/admin-api)
