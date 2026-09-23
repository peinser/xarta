# Recommand

Last verified against provider documentation: 2026-09-07

## Contract

`recommand-rest` is a tracked adapter for the provider-independent `PeppolNode`. Sender
and recipient identities, document kind, `CustomizationID`, and `ProfileID` still come
from the inspected UBL. Recommand credentials, company IDs, URLs, and webhook details
remain deployment configuration.

The adapter supports the existing UBL 2.1 Invoice and CreditNote scope using Peppol BIS
Billing 3 Profile 01. It sends Xarta's inspected XML directly through Recommand's raw XML
request shape; it does not recreate the invoice as Recommand JSON.

## Configuration

Configure Recommand through `PEPPOL_CONFIGURATIONS_CONFIG_PATH`:

```json
{
  "adapter": "recommand-rest",
  "current_revision": "development-v1",
  "revisions": {
    "development-v1": {
      "base_url": "https://app.recommand.eu/api/v1",
      "api_key": "replace-with-recommand-api-key",
      "api_secret": "replace-with-recommand-api-secret",
      "webhook_secret": "replace-with-recommand-webhook-secret",
      "companies": [
        {
          "company_id": "replace-with-recommand-company-id",
          "peppol_ids": [
            {"scheme": "0208", "identifier": "replace-with-enterprise-number"}
          ]
        }
      ],
      "timeout": 30
    }
  }
}
```

API requests use HTTP Basic authentication with `api_key` as username and `api_secret`
as password. Each sender participant maps to exactly one configured company. The company
ID becomes the tracked operation's `provider_account_reference`; the successful send
response's document ID becomes `provider_reference`. Retain old revisions until their
operations drain.

When Helm uses a fixed externally managed Secret through
`services.peppol.existingSecret`, set `services.peppol.existingSecretChecksum` to a new
value whenever `peppol.json` rotates. Content-addressed Secret names are preferred.

The configured company ID and sender identifiers are deployment choices. Xarta does not
read or synchronize company or identifier records at runtime. The actual provider
operations validate the credentials, company, and provider configuration.

## Execution

Xarta validates and inspects the UBL locally, resolves the configured company, and calls
`verify-document-support` with the recipient, full UBL document type identifier, and
Profile 01 process ID. Only a documented response with `success=true` and boolean
`isValid=false` maps to `recipient_not_registered`; malformed successful responses are
provider errors, and temporary lookup failures remain retryable.

Recommand documents no separate read-only raw XML validation operation. Xarta therefore
does not add a symmetry-only provider call. The send endpoint performs provider
validation. A documented HTTP 400 or 422 response with `success=false` and an `errors`
object maps to `invalid_document` without exposing the provider body. Other HTTP 4xx
responses remain generic provider rejections unless explicitly mapped.

The direct request is:

```text
POST /api/v1/{company_id}/send
  recipient = <scheme>:<identifier>
  documentType = xml
  document = <existing UBL XML>
  doctypeId = <full UBL document type identifier>
  processId = <UBL ProfileID>
```

Recommand documents a successful send with `sentOverPeppol=true` as technical delivery
to the recipient software. Xarta accepts that evidence only when `success=true`, `id` is
a non-empty string, `companyId` matches the configured company, and `sentOverPeppol` is
boolean. It maps `true` to `delivery_confirmed`; this does not mean viewed, approved,
business-accepted, or paid. A valid response with `sentOverPeppol=false` is a terminal
`execution_failed` because this adapter requests no email fallback and the provider does
not document a more specific Peppol result. Unknown or malformed fields never become
delivery confirmation.

## Retry And Ambiguity

Recipient support verification is read-only and retry-safe. The direct send is not.
Recommand currently documents no send idempotency key,
caller-supplied identity, duplicate-suppression guarantee, or unique lookup that can
recover a lost POST response.

Xarta durably checkpoints phase `submitting` before the POST. A definitive response is
normalized and checkpointed locally with the returned document ID, provider state, and
provider-neutral delivery state before reduction. Redelivery replays that local
observation without another provider call. A timeout, connection loss, ambiguous server
response, unusable 2xx response, or crash before the response checkpoint resolves to
`outcome_uncertain`; redelivery never blindly sends the XML again. Recommand is not
polled for document state. Recommand and Xarta do not provide exactly-once outbound
delivery at this boundary.

## Webhooks

Provision a dedicated Recommand rule manually; Xarta never mutates provider control-plane
configuration at startup:

```text
Trigger:        Document sent
Action:         Webhook
URL:            https://<xarta>/api/v1/peppol/callbacks/recommand
Signing secret: the corresponding revision's webhook_secret
```

Use a Recommand rule delivery that supplies the documented `X-Idempotency-Key`. Xarta
requires that key as the external event identity. Recommand signs the exact raw request
body as lowercase hexadecimal HMAC-SHA256 in `X-Signature: sha256=<digest>`. Xarta bounds
the body at 64 KiB, verifies the signature with constant-time comparison before trusting
routing fields, and never logs the body, signature, XML, or credentials.

After authentication, only a company/document pair matching a tracked operation pinned
to the same adapter revision is queued. The HTTP handler durably publishes a safe hint
containing only operation, event, company, and document IDs to the existing JOBS
JetStream stream, then returns HTTP 200. Its consumer rechecks the pinned identity and
applies a local provider-neutral `delivery_confirmed` observation. No provider request is
made while accepting or consuming the callback.
JetStream message deduplication and the tracking provider inbox use the Recommand
idempotency key; duplicate, late, and out-of-order callbacks cannot emit duplicate
semantic outcomes.

Recommand documents a `Document sent` rule trigger but does not currently publish its
exact outbound `eventType` wire value. Xarta therefore does not guess or route on that
value. The dedicated rule gives an authenticated callback its meaning: valid HMAC plus
an owned company/document identity is evidence that Recommand sent the document. A valid
callback for an unowned document is acknowledged with `queued=false` to avoid provider
retry storms and does not reveal whether another document exists internally.

## Playground

Use an isolated playground for the opt-in `recommand_contract` test. Isolated playground
sends are simulated, do not use the real Peppol network, and have no subscription checks
or billing. The test verifies the documented `playground.isPlayground` response; the
operator must separately ensure that the playground is not connected to the Peppol Test
Network. Configure the `RECOMMAND_CONTRACT_*` variables and set
`CONFIRM_RECOMMAND_PLAYGROUND=yes` explicitly. The contract test is excluded from normal
verification.

## Provider Links

- [Authentication](https://docs.recommand.eu/docs/authentication)
- [Send document](https://docs.recommand.eu/reference/sending/send-document)
- [Verify document support](https://docs.recommand.eu/reference/recipients/verify-document-support)
- [Working with webhooks](https://docs.recommand.eu/docs/working-with-webhooks)
- [Rules](https://docs.recommand.eu/docs/rules)
- [Playground environment](https://docs.recommand.eu/docs#playground-environment)
