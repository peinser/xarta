# Postal Bpost Integration

## Status and authority

Bpost is a possible carrier used by the Local Postal Adapter; it does not define the
generic `postal` capability. The repository currently contains contract-parameterized
domain models and pure interpretation functions. It does not contain a Bpost SEN HTTP
client, credentials/configuration loader, announcement/tracking/search/proof calls,
callback endpoint, reconciliation scheduler, or live contract tests.

Bpost ownership is explicit in
`src/xarta/services/v1/postal/adapters/local/bpost/`: `sen.py` owns SEN mappings,
reconciliation, and address feedback, while `port_paid.py` owns Port Paid profiles,
eligibility, assets, planning, and deposit grouping. The neighboring `carrier.py` owns
only provider-neutral semantic states, interpretation input, and generic outcome
mapping. Bpost exports are available only from `postal.adapters.local.bpost`.

No provider code, endpoint, status, callback, idempotency duration, search guarantee,
barcode rule, proof type, tariff, Port Betaald threshold, or deposit rule in this guide
is asserted as Bpost behavior unless supplied and approved through the applicable Bpost
contract. Examples in tests are synthetic.

## Contract-required configuration

Before enabling Bpost production, obtain and revision-control approved configuration
for the exact account, product, environment, and effective dates:

- SEN endpoints, authentication, account/correlation scope, timeouts, request/response
  limits, and retention requirements;
- customer-reference constraints and whether search supports exact, complete,
  sufficiently long-lived reconciliation;
- announcement request/response semantics, duplicate behavior, provider idempotency
  key scope and expiry, and barcode/item identity rules;
- registered product eligibility, address-feedback codes, status codes and terminality,
  tracking availability, polling limits, proof availability and formats;
- callback transport and authentication, event identity/order/replay rules, if callbacks
  are offered;
- Port Betaald mark, number, product eligibility, volume bounds, grouping, site, channel,
  deposit, and authorization requirements;
- tariffs, taxes, supplements, effective dates, and evidence retention.

Missing or ambiguous contract data fails closed. Do not infer behavior from a portal,
numeric status ordering, ordinary-mail conventions, or another Bpost product/account.
Retain every configuration revision needed by an active operation.

## Registered SEN lifecycle

The intended registered-mail integration is tracked. Before announcement, the adapter
must persist an `announcing` checkpoint, immutable customer reference, pinned
destination/configuration/account scope, source/plan identity, and request identity.
Only then may it cross the announcement side-effect boundary.

The exact ambiguous boundary is sending an announcement without receiving a definitive,
durably checkpointed response. Bpost may have created an item. On timeout, connection
loss, invalid/truncated response, or crash at this boundary:

1. Mark the operation uncertain; do not blindly announce again.
2. Search in the pinned provider account by the exact immutable customer reference, but
   only if the contract confirms that search is suitable for this reconciliation.
3. Adopt exactly one exact match and persist provider item ID and barcode.
4. Leave zero or multiple matches for manual/provider-assisted reconciliation.

`reconcile_ambiguous_announcement()` implements only the exact-match cardinality rule.
It does not call Bpost or prove that the provider's search is complete. If the contract
does not supply a reliable search within the needed retention horizon, automated
announcement retry is unsafe after ambiguity.

The provider idempotency scope and expiry are currently unknown and contract-required.
Deterministic Xarta task or customer-reference identity is not a claim of provider
idempotency. After any provider window expires, reconcile or require a manual decision;
never create a replacement registered item merely because a retry is old.

## Address feedback

`SenContractMapping` requires a nonempty revisioned mapping from provider codes to
`accepted`, `formatting_only`, `material_change`, or `unknown`. The mapping itself must
come from the confirmed contract.

Formatting-only feedback may continue only under an approved policy that preserves the
recipient identity and delivery intent. Material changes reject by default. Unknown
codes do not proceed and require reconciliation. Store original and provider feedback
as restricted evidence; do not silently rewrite the generic node or treat normalization
as caller authorization for a material address change.

## Tracking, callbacks, and proof

Provider states are mapped semantically, never ordered numerically. The implemented
semantic vocabulary is announced, carrier accepted, out for delivery, available for
pickup, delivery exception, delivered, and returned. `announced` emits no generic
carrier outcome. Unknown provider states are retained and reconciled rather than
guessed. A mapping result is only interpretation logic; an authenticated provider
observation and durable reducer are still required.

Another provider can map its own authenticated states into the same neutral carrier
semantic vocabulary and use the generic reducer. For example, a future PostNL adapter
could reuse that boundary only after its own contract mapping is verified; this design
does not claim its states, services, evidence, or retry guarantees match SEN.

Callbacks are not implemented and their authenticity is unconfirmed. Until a Bpost
contract specifies authentication and replay semantics, treat callback data only as a
hint and confirm it through an authenticated tracking read. A future callback endpoint
must bound input, authenticate before mutation, deduplicate a contract-defined event
identity, resolve the pinned account/revision, and feed the same monotonic reducer as
polling.

Periodic reconciliation is required for announced and nonterminal registered items,
unknown/out-of-order states, ambiguous announcements, pending proof, and callback hints.
The contract must define safe polling frequency and history horizon. Late observations
must not erase immutable outcomes or regress a terminal state without an explicit,
audited carrier correction policy.

`delivered` is permitted only from contract-mapped, authenticated or reconciled Bpost
evidence for the registered item. `delivery_evidence_available` requires successfully
retrieved and integrity-checked proof; delivery status alone does not imply proof exists.
Missing, expired, malformed, or unavailable proof maps only according to a confirmed
policy and must not invent evidence.

## Port Betaald controlled assets

Port Betaald is Bpost integration configuration, not caller intent. Use only an official,
approved mark asset. Do not redraw it, download it at request time, or accept one from a
DAG. A controlled profile pins:

- profile ID and revision;
- contractual/non-contractual form and PB number when contractually required;
- local asset path and SHA-256;
- measured physical width and height;
- effective start/end dates.

The implemented validator requires exact digest, dimensions, and effective date. It
does not validate visual content, placement, product eligibility, approval, or contract
status. Those remain controlled configuration and release checks. Validate profiles at
service startup and stop startup on mismatch.

The local runtime loads non-secret Bpost manual-stamp profiles from
`POSTAL_BPOST_PROFILES_CONFIG_PATH`. Each profile pins product references and operator
descriptions, policy rules by service/speed/increasing maximum weight, and an exact
Decimal tariff revision and currency. Duplicate or unknown products, ambiguous bands,
missing coverage, and missing prices fail closed. The checked-in `development-v1` EUR
prices are explicitly synthetic test data and are not Belgian tariffs.

No current local runtime loads Port Paid profiles or Bpost credentials. Those domain
primitives and the existing dormant Bpost-specific database tables remain preparation
for a future contract-backed integration, not active generic local configuration.

Eligibility policies pin service type/speed, minimum and optional maximum items,
permitted sites/channels, and grouping dimensions. The implemented planner chooses PB
only when an effective profile and matching policy exist. It may choose stamps only when
fallback is explicitly enabled. Otherwise it raises
`No contract-configured valid franking method is available`.

Registered PB eligibility is not confirmed. Configure a registered policy only after
the exact contract approves it; otherwise registered PB must fail closed. Ordinary PB
thresholds and deposit rules are also configuration, not repository defaults.
`FRANKING_MACHINE` is not operationally implemented.

The e-MassPost/Port Paid minimum is evaluated for a compatible deposit group, not one
mailpiece. A deposit batch may collect compatible items across production runs. If the
minimum is not contractually guaranteed, each letter needs a valid manual-franking
alternative. Revisioned manual rules resolve exact supplies and exact price lines into
the immutable plan consumed by the local station. The traveller is page 1 of the atomic print-ready letter PDF;
its retained region carries a Code 128 bare task UUID and human-readable UUID, never the
detachable mailing label.

## Manual e-MassPost and handover

The intended initial channel name is `manual_e_masspost`. Xarta must not scrape or
automate the e-MassPost browser UI. An authorized operator performs the portal action
and records the external deposit reference, planned/service date, site, declared volume
and grouping, operator, and authorization artifact in a separate deposit batch.

A production run is not a PB deposit batch. Grouping can include service, speed, postal
format, weight band, profile, site, channel, and service date as required by the pinned
policy. The implemented grouping function deterministically groups already supplied
items; it does not create a portal deposit, enforce the group's final minimum/maximum,
persist authorization, or prove Bpost acceptance.

The retained traveller must be removed before handover. A local handover scan proves an
operator-confirmed physical action, not Bpost acceptance. Deposit evidence proves only
the documented batch/deposit fact. Ordinary mail has no item-level tracking in the
initial scope and must never be described as delivered.

## Retry and recovery summary

| Operation | Duplicate/retry rule |
|---|---|
| PB asset/policy validation | Pure and safe to repeat; configuration mismatch stops work |
| Manual e-MassPost entry | External side effect; reconcile portal/reference before repeating |
| Registered announcement | Never repeat after ambiguity until exact contract-supported reconciliation succeeds |
| Tracking/search/proof read | Read-only retry only within contract rate/retention rules |
| Callback | Deduplicate by confirmed provider event identity; currently unsupported |
| Physical handover | Never repeat based only on a missing scan response; reconcile batch and operator evidence |

Operators should quarantine unresolved items, preserve restricted identifiers and
timestamps, query the pinned account through approved channels, and record provider and
supervisor evidence. Credential rotation must retain access needed for existing pinned
operations. No automated Bpost recovery runtime exists today.

## Unsupported scope

Bpost production use, live SEN traffic, authenticated callbacks, automatic proof
retrieval, e-MassPost automation, ordinary item tracking, electronic acknowledgement of
receipt, judicial/RP mail, Mail ID, OptiRetour, Clean Mail, Direct Mail, and unconfirmed
registered PB are unsupported. Enabling any requires code, tests, security review, and
the applicable confirmed contract.
