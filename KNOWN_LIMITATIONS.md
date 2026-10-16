# Known limitations

## Authentication and tenancy

The HTTP services do not yet enforce platform-wide authentication, authorization scopes,
or tenant ownership. Kubernetes ingress is disabled by default. Do not expose Xarta
outside a trusted network until service and caller identity policies cover every
non-health endpoint.

## Workflow delivery guarantees

JetStream and Xarta's execution outbox provide at-least-once execution. No local
transaction can atomically include both an external provider side effect and Xarta's
PostgreSQL state. Correctness at that boundary therefore depends on provider idempotency,
durable checkpoints, explicit uncertainty, and reconciliation.

## Request execution history

OpenTelemetry is Xarta's primary supported cross-service operational execution history
for document flows. A deployment relying on complete operational trace history must
enable tracing, set `OTEL_TRACES_SAMPLER=always_on`, retain traces for the required
period, and monitor its Collector and backend reliability. SDK buffering, exporter or
Collector loss, process termination, retention, sampling, and misconfiguration can all
make spans unavailable.

Archive state, tracked provider operations, outcome events, payments, and other persisted
domain records remain authoritative for their respective business facts. OpenTelemetry
is intended for operational tracing and explainability; compliance-grade evidentiary
requirements must be satisfied by the relevant durable domain records and deployment
controls.

## Archive storage

PostgreSQL and object or filesystem storage are separate transactional resources.
Durable deletion jobs and reconciliation recover known deletion work, but correctness
still depends on storage idempotency and operator monitoring when the storage backend is
unavailable for an extended period.

Each archive write attempt uses private physical allocations so delayed rollback
cleanup cannot delete a concurrent retry's committed content. A process crash,
uncertain commit, lost storage response, or cleanup failure can still leave orphan
allocations. Subsequent requests use fresh keys rather than adopting those objects;
orphan reclamation must protect in-flight/uncertain writes as well as check
committed database references before deleting bytes. A missing visible row alone
is not sufficient evidence that an allocation is safe to delete.

## Authoritative archive metadata search — planned scope

The metadata-search API is under design and is not implemented yet. Its initial
scope is deliberately limited:

- Only the current HEAD version of each logical document will be searched.
  Historical version metadata will not be searched, even when an older version
  would match a filter that the current HEAD does not.
- Only logical document metadata, as stored on the HEAD version, will be searchable.
  Representation metadata will not participate in matching.

The intended source is the authoritative archive database, covering archived
documents without requiring an explicit `search-index` workflow step. This is
separate from the existing optional search-index projections.
