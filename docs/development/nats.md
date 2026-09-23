# NATS Development Runtime

Local Compose uses NATS Server `2.14.6`; Python uses `nats-py>=2.15.0,<3`, currently
locked to `2.15.0`.

The server upgrade from `2.10.21` crosses several JetStream behavior changes. Before a
production server upgrade, verify:

- burst publishers handle JetStream `429` backpressure responses;
- custom JetStream API payloads contain no unknown fields, because newer servers validate
  API schemas strictly;
- stream storage has sufficient I/O capacity and health-check alerting;
- Raft leadership remains stable under expected load;
- TLS peers do not depend on obsolete insecure cipher suites;
- memory limits leave appropriate Go runtime headroom;
- acknowledgement and flow-control permissions cover the current `$JS.ACK.*` and
  `$JS.FC.*` subjects.

NATS `2.14` freezes affected streams and fails health checks on serious filestore I/O
errors rather than continuing unsafely. Treat this as an actionable storage incident.

The local development service starts with JetStream enabled and persists its state in the
Compose volume. When testing downgrade/rollback behavior, use a disposable copy of that
volume; crossing newer stream-state formats can trigger expensive state rebuilds.

`nats-py 2.15.0` adds lame-duck reconnection handling and fixes pull-fetch, flush, watcher,
and subscription cancellation hangs. It also performs stricter stream and consumer name
validation before API requests. Xarta's stream/consumer names are covered by the normal
NATS lifecycle tests.
