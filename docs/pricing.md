# Pricing

Pricing always receives the compiled executable DAG. A flow profile is resolved and its
typed inputs are substituted before graph traversal. Quotes bind to
`PreparedIntake.semantic_fingerprint`, not an uncompiled profile name or caller envelope.

Xarta pricing is independent from payment transport. The pricing engine produces an
immutable `PriceQuote`; x402 may later communicate and settle its selling price.

All monetary arithmetic uses `Decimal`. `estimated internal cost` is Xarta's configured
cost estimate. `selling price` is what the buyer pays:

```text
selling price = estimated internal cost × markup multiplier
```

`1.50` means selling at 150% of cost, a conventional 50% markup. `1.00` is at cost,
`0.50` sells at half estimated cost, and `0` is free. Negative multipliers are rejected.

## Bindings And Rates

Pricing uses `PricingBinding(capability, destination, adapter, revision)`. This records
the concrete adapter assumption without credentials. Rate lookup is explicit:

1. capability + adapter + destination;
2. capability + adapter.

Missing coverage fails closed. Quotes pin the pricing revision, binding assumptions,
selling price, creation time, and absolute quote expiry. For x402, those terms and the
complete server requirements are carried in a signed opaque challenge, so catalog reloads
never reprice an unexpired issued quote and no quote database lookup is required.

## Activation Bounds

A static `Node` can produce several `NodeExecution` activations. Pricing therefore
propagates conservative activation bounds through outcomes. Cardinality is explicit
capability code; unknown outcomes, cycles, and safety-limit overflows fail closed.
ExecutionAttempt retries are not additional logical activations.

Email recipient outcomes can emit once per `to`, `cc`, and `bcc` recipient, while
aggregate outcomes emit once. Current Peppol outcomes emit at most once each;
`submitted` and a terminal branch are both included because both lifecycle events can
occur. V1 sums all branches rather than solving mutual-exclusion constraints.

Bundle is a single logical activation. Each declared bundle outcome can emit at most
once. Bundle has no caller-selected destination; the server-side pricing resolver must
select its adapter and configuration revision. A matching capability/adapter catalog
rate is required. There is no implicit bundle rate or zero-cost fallback.

Postal pricing is destination- and adapter-specific. A local postal quote must pin its
physical production and franking assumptions and enforce bounded pages, sheets, weight,
envelope cost, and postage before printing. If PB and stamps are both possible, the
quote uses the more expensive configured method unless PB is contractually guaranteed
for the quoted batch. The itemized Decimal helper exists, but postal binding/catalog
integration is not yet registered, so current generic quotes must not claim postal
coverage. See `docs/postal-pricing.md`.

### Linear

For `A → B → C`, each maximum activation is one. With costs `0.10`, `0.20`, `0.30`
and multiplier `1.50`, estimated cost is `0.60` and selling price is `0.90`.

### Branching

For `A success → B` and `A failure → C`, V1 prices `A + B + C`, even when success and
failure are mutually exclusive. This deliberate overestimate is fixed-price and is not
refunded after execution.

### Multiple Outcomes

For `Peppol submitted → Archive` and `delivery_confirmed → Notify`, both downstream
operations are included because submission and terminal delivery can both occur.

### Recipient Multiplicity

For an email with three recipients and `mailbox_full → Webhook`, the Webhook maximum is
three activations, not one.
