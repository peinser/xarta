# Postal Pingen Integration Assessment

## Status and authority

Pingen is a candidate remote print-and-mail provider, not an implemented Xarta
adapter. This assessment is based on Pingen's public Post API page and OpenAPI
document version 2.0.0, reviewed on 2026-08-31. Public documentation is useful
for designing the integration, but production behavior still requires an approved
Pingen account contract and staging verification.

A future implementation belongs in `postal.adapters.pingen`, alongside the Local
Postal Adapter. It must not live under `postal.adapters.local` or reuse local
production runs, station packages, travellers, printer attempts, scans, handover
batches, Bpost SEN mappings, or Port Paid configuration. Pingen performs remote
document processing, printing, and distribution; the local adapter coordinates
physical production at a Xarta-controlled site.

## Public API lifecycle

Pingen documents a three-step intake: request a signed upload URL, upload one PDF
to that URL, and create a letter using the URL and its signature. Letter processing
is asynchronous and may take from milliseconds to several minutes. Creating with
`auto_send: false` separates validation from the explicit send operation and is the
appropriate integration boundary for Xarta. The send request selects a virtual
delivery product, print mode, and print spectrum.

The documented virtual products are `fast`, `cheap`, `bulk`, `premium`, and
`registered`; these are Pingen configuration, not additions to the generic postal
schema. Pingen maps a virtual product to a country-specific product. Public input
supports simplex or duplex and color or grayscale. A Pingen profile must fail
closed for Xarta's duplex short-edge request because the public API does not expose
an edge selection. Monochrome can map to grayscale. Product eligibility, Belgian
coverage, registered-mail behavior, sender configuration, and effective pricing
must be pinned provider configuration.

Pingen's PDF is also an addressing input: letter creation specifies a left or right
address position, while recipient and sender fields supplied as `meta_data` are
metadata. Before implementation, the contract must establish how the PDF address
is made authoritative and checked against the Xarta mailpiece recipient. A
provider-specific controlled cover page is possible, but silently trusting an
address extracted from caller content is not. Multiple Xarta document references
would need deterministic composition into one immutable PDF before upload, with
the requested document-boundary behavior preserved.

## Execution and side-effect boundaries

The adapter must use tracked execution. Upload, asynchronous validation, remote
printing, distribution, callbacks, and reconciliation remain pending after the
JetStream handler returns. Generic tracked operations should retain the pinned
destination revision, Pingen organisation/account, source digest, exact request
identity, idempotency keys, provider letter ID, and reducer state. Pingen-specific
audit records and restricted raw observations belong in a separate adapter-owned
schema, not `postal_local`.

Use a distinct deterministic idempotency key for letter creation, send, and any
other POST or PATCH operation. Pingen documents keys as 1 to 64 characters, replay
of the original status and body, an `Idempotent-Replayed: true` response header,
conflict while the original request is still processing, and retention for 24
hours. This is valuable but not exactly-once delivery:

- checkpoint the operation and key before each remote mutating request;
- retry with the same key only inside the confirmed 24-hour window and after a
  sensible delay;
- after expiry, reconcile the exact persisted Pingen letter instead of repeating
  create or send;
- if create is ambiguous and no contract-backed exact lookup can recover the
  letter, require manual/provider-assisted resolution;
- do not assume the signed object-storage PUT has the same idempotency contract as
  Pingen's POST and PATCH endpoints.

Pingen documents 300 requests per minute per user. A client must honor 429,
`Retry-After`, and rate-limit reset headers with backoff. Configurable send limits
are advertised, but their exact errors, reset behavior, and concurrency semantics
are not defined in the reviewed OpenAPI document.

Cancellation is asynchronous: the letter cancellation endpoint returns 202
Accepted. Record `cancellation_requested`, then reconcile the letter; acceptance of
the request is not the generic `cancelled` outcome. Deleting a draft is a separate
API operation and must not delete Xarta's operation or audit history.

## Events, webhooks, and reconciliation

Pingen recommends webhooks instead of continuous polling and exposes event
categories for `issues`, `sent`, `undeliverable`, and `delivered`. Letter events
contain an event UUID, code, display name, producer, location, image availability,
data, emitted time, and creation/update times. Undeliverable webhooks may include a
reason and corrected address. The API also exposes an authenticated letter-event
collection and an event-image download when an image is available.

Pingen explicitly does not publish a complete status or event-code list. Mappings
must therefore be revisioned configuration confirmed for the relevant account,
country, and product. Unknown codes, producers, or contradictory/out-of-order
observations must be retained and reconciled rather than inferred from names or
ordering.

Webhook registration accepts a 20 to 32 character `signing_key`, but the reviewed
OpenAPI document does not define a signature header, algorithm, canonical bytes,
timestamp/replay window, delivery timeout, retry schedule, event ordering, or
stable deduplication identity. Until these are contractually confirmed, a webhook
is only a bounded hint. Authenticate a follow-up read with OAuth before mutating
postal outcomes. A future callback endpoint must also authenticate before
mutation, correlate the pinned organisation and letter, durably deduplicate the
confirmed event identity, and feed the same monotonic reducer used by polling.

Periodic reconciliation remains required for ambiguous create/send/cancel calls,
nonterminal letters, callback hints, unknown or out-of-order codes, and pending
event images. The public API's list endpoints and event history are useful reads,
but their retention, completeness, correction behavior, and safe polling cadence
are not specified.

## Generic outcome mapping

No new `PostalOutcome` is justified by the public Pingen model. Provider progress
that has no exact generic meaning stays in tracked adapter state. Candidate mappings
must be confirmed by event code, producer, product, and contract rather than webhook
category alone:

| Pingen observation | Candidate Xarta interpretation | Required guard |
|---|---|---|
| Letter created or validation pending | No postal outcome | Provider intake is not physical preparation or carrier acceptance |
| Validation issue | `invalid_address`, `unsupported_service`, or `production_failed` only for a specifically mapped terminal reason | The generic `issues` category covers more than address failures |
| `sent` / transferred to distributor | `carrier_accepted` | Contract must establish that the physical mailpiece was accepted by the distributor; never map to local operator `handed_over` |
| Delivery disruption | `delivery_exception` | Confirmed event-code semantics; a temporary exception is not a return |
| `delivered` | `delivered` | Authenticated or reconciled observation whose product semantics establish recipient delivery |
| `undeliverable` | Usually `delivery_exception`; possibly `returned` only for a confirmed physical return event | A corrected address or digital return scan alone does not prove the item was returned to the sender |
| Cancellation confirmed | `cancelled` | Confirmed final provider state, not the 202 response |
| Event image available | No evidence outcome by itself | Retrieve and integrity-check it, then classify the artifact under a confirmed evidence policy |

`prepared` and `handed_over` retain their local physical meanings and should not be
emitted for upload, validation, or remote submission. `delivery_evidence_available`
requires retrieved, integrity-checked proof that the contract recognizes as
delivery evidence. A generic event image, return-mail scan, tracking event, or
marketing claim about proof of receipt is insufficient. Pingen advertises optional
proof of receipt for registered mail, but the reviewed OpenAPI paths do not expose
an explicit proof-of-receipt resource.

The current Xarta protocol limits ordinary mail to no item-level carrier outcomes
in the Local Postal Adapter. A Pingen adapter may emit a carrier outcome for an
ordinary service only after its own product contract establishes an authenticated
item-level observation; local ordinary-mail limitations must not be mistaken for a
universal carrier fact.

## Pricing and evidence retention

Pingen exposes a price calculator for country, paper types, virtual product, print
mode, and spectrum, and returns currency plus numeric price. A letter resource also
contains price, tracking number, submitted time, and selected production fields.
The calculator may return 202, while the reviewed specification does not document a
corresponding polling operation clearly enough to use as a deterministic Xarta
quote contract.

Do not put live Pingen product names or mutable calculator results into the generic
postal node. Use a pinned destination/profile mapping and treat the provider's final
charged amount as settlement/audit evidence. Any quoted-price guarantee, taxes,
discounts, currency rules, expiry, and mismatch policy require contract-backed
configuration and tests.

Retain the immutable submitted PDF digest, provider letter ID and tracking number,
request/response identities, provider event IDs and raw codes, emitted/received
times, mapped semantic revision, cancellation state, prices, and downloaded
artifact digests. Corrected addresses and return images are restricted personal
data and need explicit access and retention controls.

## Implementation gates

Before enabling production, obtain and test:

- OAuth endpoints, scopes, token lifetime, organisation isolation, credential
  rotation, and staging/production separation;
- exact address-window, sender-profile, PDF validation, paper, attachment, and
  Belgian product rules;
- complete enabled status/event-code mappings and terminality for each product;
- webhook signature and replay verification plus delivery/retry guarantees;
- create/send/cancel recovery and exact lookup guarantees beyond the 24-hour
  idempotency window;
- item-level meanings of sent, delivered, undeliverable, returned, tracking, event
  images, and registered proof of receipt;
- price, tax, send-limit, rate-limit, retention, support, and correction policies.

Staging provides deterministic filename scenarios for an undeliverable letter, an
unprintable letter, and a cancellable letter; other filenames simulate transfer to
the distributor and sent status. These are suitable for functional adapter tests,
but staging explicitly does not print or deliver physical letters. It cannot prove
production carrier evidence, physical side effects, timing, or pricing.

Sources:

- <https://www.pingen.be/en/post-api/>
- <https://api.pingen.com/documentation>
- <https://api.pingen.com/documentation/swagger-docs>
