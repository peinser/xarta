# Postal Capability

## Status and boundaries

`postal` is the provider-neutral DAG contract for producing one physical mailpiece.
The protocol model is implemented in `src/xarta/protocol/dag/postal/`; the parser and
flow-profile fields are registered. The server includes mixed adapter dispatch, a
tracked `postal.adapters.local` concrete adapter, `postal_local` PostgreSQL persistence,
station APIs, and an internal Helm deployment. The `postal` Python package owns the
service and capability, `postal.adapters` owns the adapter registry, and
`postal.adapters.local` owns provider-neutral station production and carrier semantics.
The Bpost-specific stamps, SEN, and Port Paid domain code is isolated under
`postal.adapters.local.bpost`. The runtime loads non-secret Bpost stamp product,
policy, and tariff profiles, but has no Bpost HTTP runtime or credential configuration.

Keep these layers distinct:

| Layer | Owns | Must not own |
|---|---|---|
| Generic postal protocol | Mailpiece intent, destination name, document references, recipient, sender profile reference, service, print request, outcomes | Bpost accounts, Port Betaald numbers/assets, station identity, printers, capacity, tariffs, adapter execution mode |
| Local adapter | Physical planning, resolved provider-instruction consumption, server-side operational state, capacity, packages, station transitions, and neutral carrier reduction semantics | Generic protocol semantics, provider products/weight bands/tariffs, or station-local authority |
| Production station | Verified package handling, printer submission, explicit scans | PostgreSQL access, carrier credentials, pricing, planning, workflow authority |
| Bpost integration | Stamp products, weight/service policy, tariffs, contract-specific franking, and registered announcement/tracking/proof mappings | Generic postal schema or station workflow |
| Pingen integration | Candidate remote PDF intake, print-and-mail submission, provider events, and reconciliation | Local production runs, station workflow, Bpost products, or generic protocol semantics |
| Deployment and contract configuration | Destination binding, revisions, profiles, assets, policies, credentials, tariffs | Caller-controlled DAG data |

The local, station, Bpost, and pricing details are documented separately in
`docs/postal-local-adapter.md`, `docs/postal-production-station.md`,
`docs/postal-bpost.md`, and `docs/postal-pricing.md`.
The assessment for a possible remote Pingen adapter is in
`docs/postal-pingen.md`; no Pingen runtime is implemented.

## Generic contract

One `PostalNode` is one recipient, one envelope, and one physical mailpiece lifecycle.
Batching is an adapter concern; an array of letters is not accepted. `destination` is
an optional logical name. It is not a provider endpoint or account. The deployed
destination registry must provide either the named destination or an explicit default;
the development configuration uses `local-brussels` explicitly.

The only implemented mailpiece is `letter/v1` with:

- one to 25 ordered `generate`, `render`, or `archive` document references;
- a Belgian structured address with country `BE` and a four-digit postcode beginning
  with 1 through 9;
- at least one of recipient `name` or `company_name`;
- a server-owned sender profile reference, not an inline sender;
- ordinary `priority` or `non_priority` service, or registered service with
  `acknowledgement: none`;
- optional versioned print settings: monochrome or color; simplex, duplex long-edge,
  or duplex short-edge; and continuous, new-sheet, or recto document boundaries;
- up to 32 bounded string references and 32 namespaced extension values whose encoded
  total is at most 16 KiB.

Unknown semantic fields are rejected. Document order is physical insertion order.
Omitting `print` resolves settings from the pinned local profile revision before
production.

Example:

```json
{
  "kind": "postal",
  "destination": "local-brussels",
  "mailpiece": {
    "type": "letter",
    "version": 1,
    "content": {
      "documents": [
        {
          "source": "archive",
          "archive": "legal",
          "id": "0ce71682-841d-4f84-91ba-3bed69bda212",
          "version": "2e90fe66-71cb-48fc-b57d-62cdad7c061c",
          "role": "cover-letter"
        }
      ]
    },
    "recipient": {
      "name": "Jan Janssens",
      "address": {
        "format": "structured",
        "street": "Wetstraat",
        "house_number": "16",
        "box_number": "4",
        "postal_code": "1000",
        "city": "Brussel",
        "region": null,
        "country_code": "BE"
      }
    },
    "sender": {"type": "profile", "id": "default-be"},
    "service": {"type": "ordinary", "version": 1, "speed": "non_priority"},
    "print": {
      "version": 1,
      "color_mode": "monochrome",
      "sides": "duplex_long_edge",
      "document_boundary": "continuous"
    },
    "references": {"case_id": "CASE-2026-0001"},
    "extensions": {}
  }
}
```

## Execution and delivery semantics

Execution mode is selected by the resolved adapter, never by `PostalNode`. A package
export adapter could be synchronous; an adapter that waits for physical production or
carrier feedback must be tracked. All retained revisions of one logical destination
must preserve the same execution mode.

The Local Postal Adapter uses `TRACKED` because station work, handover,
and registered-carrier observations continue after the JetStream handler returns.
All retained revisions of a logical destination are validated at startup and may not
change execution mode.

Generic outcomes are `prepared`, `handed_over`, `carrier_accepted`,
`out_for_delivery`, `available_for_pickup`, `delivery_exception`, `delivered`,
`returned`, `deposit_evidence_available`, `delivery_evidence_available`,
`invalid_address`, `unsupported_service`, `capacity_rejected`, `production_failed`,
`evidence_unavailable`, and `cancelled`. They are registered protocol vocabulary, not
all emitted at most once per execution. The local station lifecycle emits `prepared`
and `handed_over`; capacity rejection emits `capacity_rejected`; carrier outcomes are
available only to confirmed, contract-mapped observations. Runtime failures and
uncertainty continue to use Xarta's common execution outcomes.

`prepared` means that production has been completed and the item is ready for handover.
`handed_over` records an operator-confirmed physical handover. Neither means delivered.
Carrier lifecycle and evidence outcomes require authenticated or reconciled carrier
evidence.

Ordinary mail has no item-level delivery observation in the initial design. It must
never emit `carrier_accepted`, `out_for_delivery`, `available_for_pickup`, `delivered`,
`returned`, or `delivery_evidence_available` merely because it was produced or handed
over. Its tracked execution may resolve at `handed_over`; a deposit artifact proves a
batch/deposit event, not recipient delivery.

## Idempotency, retries, and ambiguity

Generic deterministic node-execution and JetStream message identities suppress some
duplicate work, but do not prove exactly-once printing, handover, or carrier side
effects. Every adapter must define its own key scope and retention period. No generic
postal idempotency expiry exists in the protocol.

For a tracked local implementation, the required scope is one immutable node execution
and pinned destination revision. Station events must additionally be unique within the
production task, event type, production generation, and handover batch where relevant.
The required retention is at least the complete operation and audit-retention lifetime;
the PostgreSQL store enforces this uniqueness. No automatic retention expiry is
configured; records remain subject to the deployment's audit-retention policy.

Safe retries stop before a side effect or replay a durably deduplicated state transition.
Ambiguous boundaries include printer submission, physical handover before its scan is
acknowledged, Bpost registered announcement, and any callback whose authenticity or
meaning has not been confirmed. Never automatically reprint, re-hand over, or
re-announce across one of those boundaries. Recover from authoritative server state and
reconcile with printer, operator, or carrier evidence.

The generic protocol has no callback endpoint or reconciliation implementation. Those
belong to a feedback-capable adapter. See `docs/postal-bpost.md` for the contract gates
that apply to Bpost behavior.

A future provider such as PostNL can translate its authenticated observations into the
neutral carrier semantic states and reuse the generic persistence, reduction, and
station production logic. That architectural reuse does not establish that provider's
status meanings, products, idempotency, evidence, or contracts as equivalent to Bpost.

## Unsupported scope

The current protocol rejects or does not model international mail, parcels, postcards,
judicial or RP mail, anonymous registered senders, electronic acknowledgement of
receipt, Mail ID, OptiRetour, Direct Mail, Clean Mail qualification, and arbitrary
inline senders. The local operational design also excludes automated inserters,
automated franking machines, e-MassPost browser automation, and offline station
operation. Unsupported requests must fail closed and must not be silently downgraded.
