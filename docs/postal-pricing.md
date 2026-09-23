# Postal Pricing

## Ownership and status

Postal cost is adapter-specific because physical production and carrier assumptions
depend on the selected destination revision. The generic `postal` node contains no
tariff, Port Betaald profile, station rate, or carrier account. Port Betaald profiles
and policy helpers are Bpost-owned under
`postal.adapters.local.bpost.port_paid`; they are not generic local pricing or franking
configuration. Bpost manual stamp products, selection rules, and postage tariffs are
owned by `postal.adapters.local.bpost.stamps`; the local station receives only resolved
provider-neutral production instructions.

`src/xarta/services/v1/postal/adapters/local/pricing.py` retains neutral exact line
arithmetic and bounded-production validation. It is not registered with the
generic pricing engine, and no postal pricing binding resolver or production tariff
catalog currently exists. A generic DAG quote therefore must not be represented as
including postal cost until that integration is implemented.

## Required binding

A postal quote must pin at least:

- capability `postal`, logical destination, adapter, and destination revision;
- local pricing/tariff revision and currency;
- sender, paper, envelope, traveller-template, site, and handling assumptions;
- service, speed, postal format, weight band, and registered supplement where relevant;
- permitted franking methods and the method guaranteed by contract/batch context, if
  any;
- Port Betaald profile/policy and deposit assumptions when PB is guaranteed;
- page, sheet, weight, envelope-cost, and postage bounds;
- quote creation and expiry under the generic pricing contract.

Credentials and PB account secrets do not belong in a quote. Missing rates or binding
coverage fail closed; there is no implicit free postal rate.

## Itemization

Configured components may include document preparation/snapshot storage, traveller
sheet and printing, content paper and monochrome/color impressions, alignment blanks,
envelope, cutting/label attachment, folding, insertion, station handling, handover,
Port Betaald postage, registered supplement, stamps, carrier reconciliation, and proof
storage. Names are catalog-defined; the current calculator accepts nonempty component
names rather than prescribing a production catalog.

Every quantity, unit price, line amount, physical measurement, and total uses `Decimal`.
Binary floating point, negative values, or inexact line amounts are rejected. Bpost's
resolver separately requires positive product unit prices and exact
`quantity * unit_price` line amounts and totals.

Port Betaald eliminates stamp allocation only; it does not make carrier service free.
Do not assume Clean Mail, Direct Mail, volume, or other discounts unless the pinned
contract and actual production satisfy them.

## Franking uncertainty

There is currently no generic local franking-price selector. Bpost stamp resolution
selects the smallest configured covering weight band and sums the exact selected product
lines. Future PB-versus-stamps quote policy belongs in a Bpost-aware pricing integration,
not in generic local pricing.

That pricing selection does not select production franking. The local franking planner
still validates the effective PB asset/profile and policy at production time. Missing or
ineligible PB either uses explicitly enabled stamps fallback or fails closed. Registered
PB behavior requires a confirmed contract and policy; no quote may assume it otherwise.

## Bounded quotes

Exact source PDFs may not exist when a quote is requested. A bounded postal quote must
therefore conservatively cap:

- content pages;
- physical sheets after boundary blanks;
- total weight;
- envelope cost;
- postage.

At production, compare the immutable plan with every bound before printing or carrier
announcement. `validate_production_bounds()` reports all exceeded dimensions and fails.
It does not issue, persist, or consume a quote. Runtime integration must also verify the
pinned pricing and destination revisions and any dimensions not represented by the
current helper, such as thickness, format, color impressions, and batch eligibility.
Never silently charge more, downgrade service/printing, or continue outside a quote.

## DAG activation pricing

Each postal outcome is declared to emit at most once per node execution in the initial
contract, but several lifecycle outcomes can occur for one registered letter. Pricing
cardinality must conservatively include every configured reachable outcome edge under
the generic V1 all-branches rule. Because generic protocol vocabulary includes carrier
outcomes that the local ordinary service will never emit, an ordinary flow that declares
such an edge can be conservatively overquoted even though runtime must not activate it.
The current pricing graph has no postal cardinality registration and therefore fails
closed before this question arises.

Retries, print attempts, and reconciliation polls are not additional logical DAG
activations, but their expected operational costs may be included in configured rates.
A supervisor-authorized reprint is an operational cost event, not automatically a new
node execution or buyer charge. Commercial treatment is policy outside the current
calculator.

## Configuration and unsupported claims

The repository ships only clearly synthetic `development-v1` Bpost EUR unit prices for
local scenarios. It ships no production postal tariffs, taxes, Bpost prices, PB thresholds,
envelope supplier values, labor rates, or discount entitlements. All are deployment and
contract inputs with effective dates and change control. The current helper does not
apply tax, currency conversion, rounding, markup, payment, refunds, or quote persistence;
the generic pricing layer owns applicable selling-price behavior once integration exists.

Until a postal calculator is registered end to end, postal quotes and x402 settlement
for postal flows are unsupported.
