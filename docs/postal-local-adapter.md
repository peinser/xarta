# Local Postal Adapter

## Scope and implementation status

The Local Postal Adapter is the server-side model for manually producing Belgian
letters at a Xarta-controlled site. `postal` is the service and capability package,
`postal.adapters` owns its adapter registry, and `postal.adapters.local` is its concrete
adapter and must not redefine the generic `postal` node.

The capability and adapter are implemented under `src/xarta/services/v1/postal/` and
`src/xarta/services/v1/postal/adapters/local/`, respectively:

- Decimal-only paper, envelope, production-plan, and quantity models;
- deterministic page, blank, sheet, impression, weight, thickness, envelope-selection,
  and production-plan digest calculations;
- daily-capacity decisions;
- deterministic ZIP package construction;
- print and three-scan state reducers;
- itemized postal pricing and production-bound validation;
- provider-neutral carrier semantic states and tracked reduction logic;
- tracked adapter and mixed NATS dispatch with pinned destination revisions;
- immutable PDF snapshotting, atomic letter construction, and station-compatible packages;
- Sqitch-managed operational persistence, locked capacity, atomic claims, print attempts,
  package acknowledgement, handover batches, and three-scan transitions;
- bearer-authenticated, site-scoped station APIs and run-scoped package tokens;
- atomic local projection and generic tracked-outcome reductions.

Not implemented are a contract-enabled Bpost HTTP client, callback endpoint,
announcement/reconciliation scheduler, production Port Betaald asset placement, and
automatic proof retrieval. The local manual production path is executable, but it is
not a production carrier integration until those contracts and controlled assets exist.
The Bpost SEN and Port Paid domain primitives live under
`postal.adapters.local.bpost`, not in the provider-neutral adapter modules.

## Required execution contract

The adapter execution mode is `TRACKED`. A letter remains pending after initial
admission while documents are prepared, a station prints it, operators scan it through
physical production and handover, and registered mail may await carrier observations.
The initial handler must durably admit and checkpoint the operation before returning
`waiting_feedback`.

On duplicate JetStream delivery, tracking admission must load the same node execution
and local operation. A live attempt is delayed, a waiting operation is acknowledged
without resubmission, and a terminal operation is acknowledged without invoking the
adapter. An expired attempt lease may resume only from durable checkpoints. It must not
snapshot again, allocate another physical task, print again, or announce another
registered item. The postal mixed consumer and tracking admission implement this
behavior.

The intended state concepts are separate:

- one generic node execution identifies one activation;
- one generic tracked operation owns routing and immutable outcomes;
- one local operation and production plan own the mailpiece;
- a production run is a station work assignment;
- a handover batch is a physical grouping;
- a Port Betaald deposit batch is a contract/franking grouping and can span runs.

Local projection changes and generic tracked-operation
outcomes must commit in one PostgreSQL transaction. PostgreSQL must not be used as a
handoff queue for synchronous successors; deterministic successors publish directly to
JetStream after commit.

## Planning contract

Before a production task can become available to a station, the adapter must resolve
each ordered source to PDF, persist the exact bytes immutably, record its concrete source
version and SHA-256, inspect page count, and pin the resolved print and local profile
revision. Retries and
reprints must use those snapshots rather than re-rendering or resolving a newer source.
Snapshot, print resolution, and Bpost manual-stamp instructions are implemented.
Production stock values and every provider tariff remain fail-closed deployment
configuration and require production review before use.

Simplex consumes one sheet per content page. Duplex consumes two occupied sides per
sheet. For duplex, non-continuous boundaries insert a blank side after each odd-length
content-source document except the last. Both currently implemented non-continuous enum
values have that calculation; no additional distinction beyond recto alignment is
implemented. The print-ready PDF always starts with the one-page traveller. Simplex
content follows it directly; either duplex mode adds an intentional blank page 2 so
content starts recto on page 3. Traveller stock remains excluded from content weight.
Envelope selection chooses the lowest configured envelope + carrier + handling
cost among physically valid candidates, with stable identity/revision tie-breaks.

All money and physical quantities use `Decimal`; binary floats are rejected. Profiles
and tariff data are configuration assumptions and must use measured, effective,
revisioned values. The code does not ship production values.

The local profile contains only `provider.adapter` and `provider.profile` references.
An injected provider resolver receives service, speed, and calculated weight. The Bpost
resolver owns revisioned stamp products, descriptions, strictly increasing weight bands,
policy, and exact Decimal EUR tariffs. It returns provider-neutral immutable supplies and
pricing lines. The plan snapshots provider/profile revisions, franking method, policy and
tariff revisions, currency, exact unit prices, line amounts, and total postage under
`franking.supplies` and `franking.pricing`. Missing coverage or tariff fails before
production; shared local code never selects a Bpost weight band.

Port Paid/e-MassPost is batch-level. Its configured minimum item count must be met by a
compatible deposit group; it cannot be inferred from one letter or one production run.
Until an approved asset, eligibility policy, and qualifying group are present, the
planner uses an explicitly configured manual-stamp alternative or fails closed.
These Port Paid rules remain Bpost-owned and dormant. Manual Bpost stamps are active
through the provider resolver registry; the local adapter does not parse their policy.

## Capacity and production runs

Capacity belongs to the adapter, not the node. `daily_admission_limit` means:

- `-1`: unlimited;
- `0`: no admission, followed by the configured queue or reject overflow policy;
- positive `N`: at most `N` items admitted for that local service date.

The repository derives the local service date in the configured IANA timezone, locks the
capacity-day row, and inserts a unique reservation before incrementing admission. Queue
overflow remains `waiting_capacity`; automatic queue promotion and reservation release
are not implemented.

Production claims must be side-effecting, station-scoped, and idempotent. The proposed
claim key scope is `(station_id, Idempotency-Key)`, retained for at least the active run
and its operational audit lifetime. Claiming must use one transaction and
`FOR UPDATE SKIP LOCKED`, apply run limits, and preserve stable order. Package rendering
must happen outside the claim transaction. The claim endpoint implements this contract;
pre-publication build failures cancel the run and return still-claimed tasks to pending.
An empty claim is retained for deterministic replay but immediately cancelled so polling
does not accumulate active runs.

## Packages and station handoff

The server snapshots exact source PDFs, resolves and persists the immutable production
and Bpost instructions, claims tasks, and creates deterministic authenticated ZIP bytes.
The only schema is `xarta.postal.production-package/v3`. Its top-level ordered letters
contain stable job identity, source descriptors, print instructions, and pinned traveller
instructions. The ZIP contains only `manifest.json`, `checksums.sha256`, and source PDFs;
the server rechecks source digest, size, and page count before packaging and stores the
run-owned package reference directly on the run. It creates no package or letter artifact.

The station verifies the package, renders the exact A4 traveller, merges ordered sources,
applies document-boundary blanks, and composes one retained atomic letter PDF. Printer
selection is never in the manifest: `POSTAL_STATION_PRINTER` owns the local CUPS queue.

The PostgreSQL kind enum is defined by the squashed pre-release Sqitch change `00023`.
Current code and the database emit and accept only `letter`.

The required publication recovery policy is:

| State | Automatic action |
|---|---|
| Build failed before publication | Clean partial artifacts and release safely |
| Published but not acknowledged | Keep assigned; only the same station may redownload/resume |
| Package acknowledged | Never automatically return to pending or assign elsewhere |
| Print result uncertain | Stop; supervisor reconciles and explicitly authorizes a new generation |

Publication metadata and acknowledgements are persisted. Published runs remain assigned;
acknowledged runs are never automatically returned to pending.

## Printing, scans, and outcomes

A print attempt, including the rendered SHA-256 and byte count, must exist server-side
before crossing the printer submission boundary.
Stable names have the form
`postal/<run>/<sequence>/<kind>/<generation>/<attempt>`. The server reducer distinguishes
`pending`, `submitting`, `printing`, `completed`, `uncertain`, and `failed`. A timeout,
process interruption, or lost response after submission can mean that paper was printed;
mark it uncertain and do not automatically retry. A supervisor-approved reprint creates
a new generation; stale-generation scans are invalid.

Physical production requires an explicitly selected scan mode:

1. `start_production`: `ready_for_processing` to `processing`, with no outcome.
2. `ready_for_handover`: `processing` to `ready_for_handover`, emitting `prepared`.
3. `confirm_handover`: `ready_for_handover` to `handed_over`, emitting `handed_over`.

The traveller renders a Code 128 barcode directly through `python-barcode`; pyHanko is
used only for PDF reading, writing, and composition. The payload is the bare task UUID,
and the same UUID is human-readable retained text. Neither the barcode graphic nor UUID
may enter the detachable mailing label. The exact A4 page uses bounded marked regions
for the retained header, barcode, four operator sections, dashed cut line with two vector
scissors, and detachable label. Content that cannot fit is rejected rather than
overlapping, truncating, or shrinking. Scans accept only the bare UUID.

The repository and generic provider inbox durably deduplicate events. The tracking-store
transaction mutation hook commits the append-only local event, local projection,
tracked state, outcome, and deterministic successor identities together. Wrong stage,
generation, or handover batch fails rather than inferring
or skipping a transition. Each outcome must emit at most once per node execution.
Confirming the final task in a handover batch completes that batch and its station run;
each handed-over task's assignment is completed. This closes station work only. A
registered tracked operation remains open for its separate carrier lifecycle.

Ordinary letters resolve no later carrier delivery state in the initial design. They
must not emit `delivered`; handover and deposit evidence are the strongest local facts.
Registered letters remain open for the contract-configured carrier lifecycle.

## Idempotency and retry matrix

| Boundary | Scope and expiry | Retry rule |
|---|---|---|
| JetStream task admission | Full node-execution identity; tracking retention | Duplicate active/terminal executions must not create another local operation |
| Production claim | Station + client idempotency key; active-run/audit lifetime | Replay returns the same run |
| Package acknowledgement | Run + package/manifest SHA-256 + byte count; run lifetime | Exact duplicate is a no-op; mismatch is an integrity error |
| Print attempt event | Attempt + event identity; operation audit lifetime | Replay the recorded transition; never infer printer completion |
| Scan | Task + mode + generation + applicable handover batch; operation audit lifetime | Exact duplicate is a no-op; conflicting replay fails |
| Handover batch | Station + client idempotency key; operation audit lifetime | Exact task/date/operator replay returns the same deterministic batch; changed input fails |
| Bpost announcement | Immutable customer reference in the configured provider account; contract-defined search horizon | Reconcile an ambiguous call before any repeat |

These scopes are enforced by persistence and API boundaries. No automatic expiry exists
today; operators must retain rows for the required operational and audit lifetime.

## Operational logging

Postal lifecycle events are structured JSON. Adapter admission events carry `flow_id`,
`correlation_id`, `node_id`, `node_execution_id`, destination, adapter, configuration
revision, tracked `operation_id`, and local operation identity. Station-facing events
record production-run, print-attempt, task, and handover-batch identities. Authoritative
scan transitions reconnect those station identities to the originating flow and record
`previous_state`, `next_state`, `scan_mode`, and generation after the tracking and postal
mutations commit.

The primary events are `capability_adapter_selected`,
`capability_lifecycle_state_changed`, `postal_operation_prepared`,
`postal_documents_snapshotted`, `postal_local_submission_committed`,
`postal_production_run_claimed`, `postal_production_package_acknowledged`,
`postal_print_attempt_created`, `postal_print_state_changed`,
`postal_handover_batch_created`, and `postal_physical_state_changed`. Empty station polls
are not logged at info level. Grafana/Loki should parse UUIDs from JSON rather than use
them as labels; for example:

```logql
{app=~"$xarta_services"} | json | correlation_id="$correlation_id"
```

Do not log station or run tokens, idempotency keys, traveller barcodes, addresses,
recipient data, package or document contents, storage paths, printer details, or operator
references.

## Operational recovery

Recovery must start from authoritative server state, immutable artifacts, and external
evidence, not a station cache or operator memory. Keep the operation assigned while a
package or printer result is ambiguous. Compare package checksums, generation, stable
printer name/CUPS state, scans, retained traveller, handover batch, and carrier records.
Record the evidence and supervisor decision. Never repair ambiguity by silently making
a second physical letter.

The adapter has no callback or reconciliation runtime today. Bpost callbacks, if later
enabled, must be authenticated according to a confirmed contract and deduplicated; until
then they are hints requiring an authenticated carrier read. Periodic reconciliation is
required for registered operations, unknown provider states, ambiguous announcements,
and pending proofs.

Provider-specific integrations may normalize confirmed observations into
`CarrierSemanticState` and then reuse the local carrier reducer and generic carrier
tables. A future provider such as PostNL must define and verify its own mapping and
contract behavior; sharing the neutral station path does not imply SEN or Port Paid
equivalence.

## Unsupported scope

The local design currently excludes international production, parcels, postcards,
judicial/RP mail, acknowledgement of receipt, automated insertion/franking,
e-MassPost browser automation, offline stations, and automatic recovery across an
ambiguous physical side effect. `FRANKING_MACHINE` is a domain enum only; no planner or
station implementation supports it.
