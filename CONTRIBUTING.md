# Contributing to Xarta

Thank you for considering a contribution. This guide explains how to report problems,
propose changes, and get a pull request merged.

## Questions and Bug Reports

Use [GitHub issues](https://github.com/peinser/xarta/issues) for questions, bug reports,
and feature requests. Search existing issues first.

A useful bug report includes:

- the Xarta version (`VERSION`) or commit, and how you run it (Dev Container, Compose,
  or Helm);
- the request or flow that triggers the problem, reduced to the smallest example that
  still fails;
- what you expected and what happened, including relevant log lines.

Remove credentials, document contents, and personal data from anything you post.

## Security Issues

Do not report vulnerabilities in a public issue. Email
[security@peinser.com](mailto:security@peinser.com) instead.

## Licensing of Contributions

Xarta is available under the [AGPLv3](LICENSE) and under a commercial license from
Peinser BV. To keep offering both, Peinser BV needs the right to distribute every
contribution under either license. Before your first pull request can be merged, you will
be asked to sign a Contributor License Agreement (CLA) that grants those rights.

Only submit work that you wrote yourself or that you have the right to contribute.

## Before You Start

Open an issue before starting a large change, a new provider adapter, or anything that
changes a public API, database schema, or workflow guarantee, so we can agree on the
approach first. Small fixes and documentation improvements can go straight to a pull
request.

Xarta has not had a public release yet. Until it does, replace obsolete APIs, schemas, and
configuration directly instead of adding backward-compatibility layers.

## Development Environment

The supported environment is the repository's [Dev Container](https://containers.dev/);
see the [Quick Start](README.md#quick-start). Container creation runs `make setup`.
Outside the Dev Container, install the locked dependencies and run the checks with:

```console
uv sync --frozen --all-groups
make verify
```

Run `make help` to list all targets.

## Making Changes

- Keep each pull request focused on one change.
- Add or update focused tests for every behavioral change.
- Run `make verify` before pushing. It checks Black formatting, Ruff, mypy, the test
  suite, Bandit, and the flow-profile lock. `make format` fixes formatting.
- Run `make helm-verify` after Helm or Kubernetes changes, and `make docker-validate`
  after production container changes.
- Change dependencies in `pyproject.toml` and run `make lock`. Never edit `uv.lock` by
  hand.
- Database changes use [Sqitch](https://sqitch.org/). Add matching `deploy`, `revert`,
  and `verify` scripts and append the change to `sqitch.plan`, in `db/` for the
  application schema or `db/archive/` for the archive schema. `make db-deploy` applies
  them locally.
- Published flow-profile versions are immutable. A semantic change needs a new version,
  and `examples/configuration/flow-profile-lock.json` is append-only. See
  [Flow profiles](docs/flow-profiles.md).
- For end-to-end changes, run the relevant `make scenario-*` target or
  `make scenario-all`. Stop any Xarta process you started manually first; scenarios start
  and stop their own.
- Never commit credentials, tokens, private keys, or real personal data. Files under
  `.dev/conf/` contain placeholders only.

### Tests With External Side Effects

Contract tests against external services are skipped unless configured. The
`doccle_acc`, `e_invoice_be_contract`, `recommand_contract`, and `resend_contract` pytest
markers call real provider environments that can have side effects; run them only against
sandbox accounts you control.

`make scenario-postal-local-printer` prints physical mail and requires
`CONFIRM_PHYSICAL_PRINT=yes`. Never run it on a printer you are not authorized to use.

### Workflow Execution Rules

Workers and provider adapters must follow the execution model described in
[Tracked capabilities](docs/tracked-capabilities.md):

- A node whose final outcome is known before its JetStream handler returns is
  synchronous. JetStream owns its delivery, retries, and deduplication. Do not create
  PostgreSQL workflow state for it, even when it follows a tracked node.
- Use PostgreSQL tracking only for operations that stay pending after the handler
  returns, such as provider callbacks and reconciliation.
- Acknowledge a synchronous request only after its work and all deterministic successor
  publications have succeeded.
- Document each adapter's execution mode, idempotency scope and expiry, ambiguous
  side-effect boundary, duplicate-delivery behavior, retry safety, and callback or
  reconciliation support. A deterministic task identity does not make an external side
  effect exactly-once.

## Code Style

Black and Ruff enforce formatting and import order. Beyond that:

- Write explicit code for human maintainers. Prefer the standard library and existing
  dependencies over new ones, and avoid abstractions that current call sites do not need.
- Domain and protocol code raises domain exceptions such as `ValueError`. Translate them
  into HTTP errors once, at the transport boundary.

## Commit Messages

Write a short, capitalized subject in the imperative mood without a trailing period, for
example `Fix transform generated output collision check`. Use the body to explain why when
the reason is not obvious from the change itself.

## Pull Requests

Describe what the change does and why, link the related issue, and list the checks and
scenarios you ran. CI runs `make verify`, the Helm checks, and the container validation
build for every pull request. A maintainer reviews each pull request before it is merged.
