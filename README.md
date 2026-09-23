# Xarta

Xarta is an asynchronous document workflow service. It turns a business request such as
"render this invoice, sign it, archive it, and send it" into a validated graph of work,
then runs that graph across independently deployable services.

The project exists to keep document policy out of application code. Callers describe
what should happen; deployment-owned configuration decides which provider, credentials,
storage backend, retry policy, and immutable configuration revision will do it. For
common workflows, versioned **flow profiles** hide the graph entirely and expose only
the business inputs a caller is allowed to provide.

> [!WARNING]
> Xarta is pre-release software (`0.0.10-dev`). It does not yet provide platform-wide
> authentication, authorization, or tenant isolation. Do not expose its HTTP services
> outside a trusted network. Read [Known limitations](KNOWN_LIMITATIONS.md) before
> evaluating a deployment.

## What Xarta Does

- Builds document flows as directed acyclic graphs (DAGs), or exposes exact-version,
  server-owned flow profiles into those graphs.
- Renders documents with configured template-engines. For instance, Jinja, Scriptura, or Gotenberg engines.
- Generates, bundles, signs, versions, archives, and expires documents.
- Delivers through email, SFTP, webhooks, Doccle, and Peppol. Tracked postal delivery is
  experimental and not ready for production mail; see [Postal](docs/postal.md#status-and-boundaries).
- Indexes document metadata into PostgreSQL or Elasticsearch-compatible stores such as
  OpenSearch.
- Calculates graph-based prices and can protect intake with x402 payments.
- Exposes a separate [Model Context Protocol (MCP)](docs/mcp.md) facade for workflow
  discovery, preparation, submission, and archive retrieval.

Capabilities are deployment-specific. An intake service advertises only the node kinds
its workers can actually execute.

## How It Works

1. A client submits a complete DAG or typed inputs for an exact flow-profile version.
2. Intake validates the whole request and checks that every required capability is
   available. Multipart uploads are persisted before execution is accepted.
3. If enabled, Xarta prices the compiled graph and settles the x402 payment.
4. Intake publishes a deterministic root task to NATS JetStream and returns `202` only
   after JetStream acknowledges it.
5. Capability workers process nodes and publish deterministic successors for the
   outcome selected by the graph.
6. Operations that remain pending after a worker returns, such as provider callbacks or
   physical mail, are tracked in PostgreSQL and reconciled to a terminal outcome.

```text
client -> intake -> NATS JetStream -> capability workers -> providers and storage
             |                               |
             +-> pricing / x402              +-> PostgreSQL tracking
                                             +-> OpenTelemetry traces
```

Execution follows **routing slip choreography**, the pattern without a central orchestrator. Every task message carries the part of the graph
that remains, and each worker publishes the successors for the outcome it produced.
PostgreSQL only tracks operations that stay pending after a worker returns.

Delivery is **at least once**, not exactly once. Deterministic identities make unchanged
retries safe inside Xarta, but external side effects still depend on provider
idempotency, checkpoints, and reconciliation. See
[Intake acceptance](docs/intake-acceptance.md) and
[Tracked capabilities](docs/tracked-capabilities.md) for the precise guarantees.

## Quick Start

The supported development environment is the repository's
[Dev Container](https://containers.dev/). It provides Python 3.13, `uv`, PostgreSQL,
NATS, Dragonfly, MinIO, OpenSearch, Sqitch, Helm, and the other tools used by the
Makefile. You need Git, Docker, and an editor or CLI that supports Dev Containers.

```console
git clone https://github.com/peinser/xarta.git
```

Open the clone in its Dev Container. Container creation runs `make setup`, which:

- installs the dependencies pinned in `uv.lock`;
- checks the local infrastructure services;
- creates development-only signing certificates;
- deploys the application and archive databases; and
- reconciles the NATS streams.

Run `make setup` yourself if container setup was interrupted.

> [!NOTE]
> The development Jinja template engine image defaults to the private
> `harbor.peinser.com` registry. Without access to it, set `JINJA_ENGINE_IMAGE` to an image
> you can pull before opening the Dev Container.

### Full Development Runtime

```console
make dev
```

`make dev` repeats setup and starts all services with hot reload. Use `make standalone`
when setup is already complete. Development provider files under `.dev/conf/` contain
non-working placeholders, so normal development cannot accidentally contact Doccle,
Peppol, SFTP, or Resend accounts.

Useful local endpoints include:

| Endpoint | Purpose |
| --- | --- |
| `GET /.info/healthz` | Process health |
| `GET /.info/readyz` | NATS and PostgreSQL readiness |
| `GET /api/v1/intake/capabilities` | Deployed DAG node contracts and JSON Schema |
| `GET /api/v1/intake/profiles` | Available flow profiles |
| `POST /api/v1/intake/prepare` | Validate, normalize, and optionally price a flow without running it |
| `GET /agents` | Human-readable guide to the agent-facing API |
| `GET /agents.md` | Compact machine-readable agent guide |

There is not yet an OpenAPI UI. [`examples/`](examples/) contains protocol and
configuration examples.

The MCP facade runs separately against the development runtime:

```console
make mcp
```

MCP clients can then connect to the stateless Streamable HTTP endpoint at
`http://localhost:8001/api/mcp`.

## Development

Xarta supports Python 3.11 through 3.13; local development and production images use
Python 3.13. [`uv`](https://docs.astral.sh/uv/) manages dependencies, and the committed
`uv.lock` is authoritative.

```console
uv sync --frozen --all-groups
uv run --frozen pytest
make verify
```

`make verify` checks Black formatting, Ruff linting, mypy, tests, Bandit, and immutable
flow-profile data. Run `make format` to apply Python formatting. Use `make lock` only for
intentional dependency updates; do not edit `uv.lock` manually.

Behavioral scenarios live in `tests/scenarios/` and own the local application processes
they exercise:

```console
make scenario-preview
make scenario-bundle
make scenario-generate-sign-archive
make scenario-search
make scenario-flow-profile
make scenario-postal-local
make scenario-x402-paid-intake
make scenario-all
```

`scenario-all` contains deterministic local scenarios. Live provider contract tests,
real-chain payment tests, and physical CUPS printing are deliberately opt-in and can
have external side effects. See `make help` before running specialized targets.

## Deployment Artifacts

The repository contains a non-root production image and Helm charts for split-service
Kubernetes deployments. The release workflows publish images and charts to
`ghcr.io/peinser/xarta`. They are deployment building blocks, not a turnkey
distribution: operators must supply NATS, databases, storage, secrets, provider
configuration, and an OpenTelemetry Collector where required.

Validate those artifacts locally with:

```console
make docker-validate
make helm-verify
```

Build the core image with:

```console
make docker-build IMAGE_REPO=example/xarta IMAGE_TAG=dev
```

Database changes are managed by [Sqitch](https://sqitch.org/). Main application and
archive schemas are separate projects under `db/` and `db/archive/`; `make db-deploy`
deploys both in the development environment.

## Observability

Xarta emits structured lifecycle logs and can export traces over OTLP/HTTP. Tracing is
disabled unless `OTEL_EXPORTER_OTLP_ENDPOINT` or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set. OpenTelemetry is Xarta's primary supported
cross-service operational execution history. Deployments relying on complete operational
trace history must enable tracing, set `OTEL_TRACES_SAMPLER=always_on`, retain traces for
the required period, and monitor the reliability of their Collector and backend.
Document contents and payloads are
intentionally excluded from telemetry.

Archive state, tracked provider operations, outcome events, payments, and other persisted
domain records remain authoritative for their respective business facts. OpenTelemetry
is intended for operational tracing and explainability; compliance-grade evidentiary
requirements must be satisfied by the relevant durable domain records and deployment
controls.

Start the optional local Collector and Jaeger services with:

```console
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
  OTEL_TRACES_SAMPLER=always_on \
  docker compose -f .dev/compose.yml --profile observability up -d \
  app_xarta_core otel-collector jaeger
```

Jaeger is available at [localhost:16686](http://localhost:16686). Search its tags for
`xarta.flow.id=<uuid>`, `xarta.correlation.id=<uuid>`, or
`xarta.operation.id=<uuid>`. Provider callbacks may start separate traces correlated by
the durable operation ID. Trace links are deliberately deferred unless operational use
shows that they add material value.

## Repository Guide

| Path | Contents |
| --- | --- |
| `src/xarta/` | Services, protocol types, adapters, storage, tracking, pricing, payments, MCP, jobs, and entry points |
| `tests/` | Unit, integration, contract, and end-to-end scenario tests |
| `apps/postal-station/` | Headless client for controlled local postal production |
| `docs/` | Capability contracts, failure semantics, and operations guides |
| `examples/` | Example protocol requests and versioned configuration |
| `db/` | Sqitch changes for application and archive databases |
| `docker/` | Production container definitions |
| `k8s/helm/` | Core and database-tool charts with render tests |
| `.dev/` | Local Compose services, safe placeholder configuration, and runtime artifacts |

## Documentation

Start with these documents:

- [Flow profiles and GitOps deployment](docs/flow-profiles.md)
- [DAG capability and adapter contracts](docs/dag-capabilities.md)
- [Intake acceptance and retry semantics](docs/intake-acceptance.md)
- [Archive versioning](docs/archive-versioning.md)
- [Temporary storage](docs/temporary-storage.md)
- [Dead-letter handling](docs/dead-letter-handling.md)
- [PDF signing](docs/signing.md)
- [Search indexing](docs/search-indexing.md)
- [Pricing](docs/pricing.md) and [x402 payments](docs/x402.md)
- [MCP](docs/mcp.md)
- [Postal capability and readiness](docs/postal.md)
- [Development guides](docs/development/)

## Security

Do not report vulnerabilities in a public issue. Send security reports to
[security@peinser.com](mailto:security@peinser.com). Until authentication and tenancy
controls are complete, all non-health endpoints must remain on a trusted network.

Development credentials and certificates in this repository are intentionally local and
must never be promoted to production. Signing policies shipped in `.dev/conf/` are for
development and integration testing, not a production assurance claim.

## Contributing

Contributions are welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first: open an issue
before undertaking a large change, add focused tests for behavioral changes, and run
`make verify` before submitting a pull request. Contributors sign a Contributor License
Agreement before their first pull request is merged.

## License

Xarta is available under the [GNU Affero General Public License v3.0](LICENSE)
(AGPLv3).

You may use, modify and redistribute Xarta under the terms of the AGPLv3.

Organisations that cannot or do not wish to comply with the AGPLv3, or that require
proprietary usage rights, commercial support, warranties or SLA terms, may obtain a
commercial license. Contact [licensing@peinser.com](mailto:licensing@peinser.com).
