# Local Postal Development and Testing

## Renderer model

There are two rendering stages in the local postal flow:

- The development Jinja template-engine container renders each `generate` node's
  source PDF. The devcontainer starts this service from `.dev/compose.yml`, and the
  scenario uses it through the configured template-engine endpoint.
- `postal_station.rendering.Renderer` renders the traveller and composes the source
  PDFs into the atomic letter PDF. It runs in process as part of `PostalStation`;
  there is no separate renderer server or command to start.

The station is a headless embeddable application with one-shot and development-mock
CLIs. For development, use the postal scenario to exercise one self-contained flow, run
`make station` once, or run `make station-mock` alongside a manually started Xarta
server to drain postal work continuously.

## Prerequisites

Run these commands inside the development container. Its lifecycle starts PostgreSQL,
NATS, Dragonfly, MinIO, OpenSearch, and the Jinja template engine.

Install locked dependencies and initialize the local services once:

```sh
make setup
```

If setup has already run, check the required sidecars with:

```sh
make services-check
```

The scenario owns port 8000. Stop any Xarta process started with `make standalone` or
`make debug` before running it. The scenario reports a clear error rather than stopping
an existing process when the port is occupied.

## Safe end-to-end flow

Run the generate, archive, postal production, rendering, fake-printing, scanning, and
handover flow with:

```sh
make scenario-postal-local
```

No physical printer is used. The scenario:

1. Deploys the current database changes and reconciles NATS streams.
2. Starts one local Xarta application on `127.0.0.1:8000`.
3. Submits three distinct generate -> archive -> postal flows.
4. Generates two source PDFs per flow and verifies them against their exact archive
   document versions.
5. Claims one production run and downloads and verifies its source-only v3 package.
6. Runs `PostalStation`, which renders one atomic traveller-and-content PDF per job.
7. Submits all three PDFs sequentially to `FakePrinter`.
8. Simulates and deduplicates production, ready-for-handover, and handover scans.
9. Verifies package, database, archive, print, and workflow invariants and then stops
   Xarta.

## Inspecting output

Scenario artifacts are retained under:

```text
.dev/runtime/scenarios/postal-local/
```

Useful outputs are:

| Path | Contents |
|---|---|
| `results.json` | Flow IDs, run ID, archive versions, package checks, rendered files, and database invariants |
| `application.log` | Captured Xarta process output |
| `production-package.zip` | Authenticated source-only package downloaded by the station |
| `extracted-package/manifest.json` | Strict production-package v3 manifest |
| `extracted-package/letters/` | Ordered source PDFs from the package |
| `archive-downloads/` | Source versions downloaded independently from Archive for equality checks |
| `station-cache/<run-id>/rendered/` | Atomic traveller-and-content PDFs produced by the station renderer |
| `station-cache/<run-id>/journal/` | Per-job durable printer-submission journals |

`results.json` records the exact generated paths and SHA-256 values. A successful run
also reports `printer: "fake"`, three printer submissions, no server-rendered letter or
package artifacts, and an empty application error list.

## Focused renderer tests

Run all postal-station unit tests without starting Xarta or submitting to CUPS:

```sh
uv run --frozen pytest -c pyproject.toml apps/postal-station/tests
```

Run only traveller rendering and station orchestration tests with:

```sh
uv run --frozen pytest -c pyproject.toml \
  apps/postal-station/tests/test_traveller.py \
  apps/postal-station/tests/test_application.py
```

These tests use temporary packages and fake APIs/printers. They cover deterministic
rendering, PDF layout, sequential submission, rendered metadata, and recovery journals.

The Jinja source renderer can be tested independently with:

```sh
make scenario-preview
```

## Running Xarta manually

Initialize the environment and run all Xarta services with hot reload:

```sh
make dev
```

The health endpoint is `http://127.0.0.1:8000/.info/healthz`. `make dev` performs setup
and then starts Xarta; it does not start the station. `make standalone` starts the same
Xarta application without repeating setup.

Once a submitted postal flow has reached the pending production state, use another
terminal to claim and process one production run with a fake printer:

```sh
make station
```

The command uses the development station identity and local server by default. Override
them with `POSTAL_STATION_API_URL`, `POSTAL_STATION_ID`,
`POSTAL_STATION_API_TOKEN`, and `POSTAL_STATION_CACHE_DIR`. It claims once and exits; an
idle result means no eligible postal tasks were available. Fake mode still reports the
print attempts as completed to the local server, but performs no CUPS submission. The
rendered PDFs and journals remain under `.dev/runtime/postal-station/<run-id>/`.

To simulate the full local postal lifecycle continuously, run this in another terminal:

```sh
make station-mock
```

Mock mode repeatedly claims every eligible supported letter, verifies its source-only
package, renders its traveller and atomic PDF, submits it to `FakePrinter`, reports print
completion, and simulates the three scan stages through one handover batch per run. It
supports the same color, monochrome, simplex, duplex, document-boundary, document-count,
ordinary, and registered inputs as the production package path. Ordinary tracked flows
resolve at handover; registered flows finish station work but remain open for carrier
feedback.

The mock is structurally fake-only: `--mock` and `--cups` are mutually exclusive and the
mock composition path never constructs `CupsPrinter`. It does not inspect
`CONFIRM_PHYSICAL_PRINT`. Rendered files and journals remain under
`.dev/runtime/postal-station-mock/<run-id>/` by default. Configure the idle/retry delay
with `POSTAL_STATION_MOCK_POLL_INTERVAL_SECONDS` and the local service date timezone with
`POSTAL_STATION_TIMEZONE`, which defaults to `Europe/Brussels`.

`SIGINT` and `SIGTERM` interrupt an idle wait immediately. If a run is active, normal
shutdown drains its fake print and scan cycle before exiting. API failures are retried
with the same claim, package, print, scan, and handover identities while the process is
running. Run tokens are not yet recoverable after process termination, so do not treat
this development mock as an unattended production station.

For the maintained self-contained flow that creates its own tasks and uses its own Xarta
process, stop `make dev` and use `make scenario-postal-local` instead.

Do not run a scenario while this manually started server is listening on port 8000.

## Physical CUPS test

The physical test submits exactly one rendered atomic letter to an explicitly configured
CUPS destination. Verify the destination and obtain authorization before running it.

```sh
export POSTAL_STATION_PRINTER=my-cups-queue
lpstat -p "$POSTAL_STATION_PRINTER"
CONFIRM_PHYSICAL_PRINT=yes make station-cups
```

This command causes a physical side effect. It limits the claim to one task and passes
its rendered job to `lp -d`. It does not prove physical completion because the current
CUPS backend does not poll the printer. Scanning and handover remain explicit operator
or embedding-UI actions.

For the guarded self-contained scenario that creates one flow, prints exactly one letter,
and simulates scans after CUPS acceptance, use:

```sh
CONFIRM_PHYSICAL_PRINT=yes make scenario-postal-local-printer
```

Artifacts are retained separately under:

```text
.dev/runtime/scenarios/postal-local-printer/
```

Both physical targets are excluded from `make verify` and `make scenario-all`. Never run
either without explicit authorization for the configured printer.

## Troubleshooting

- `Port 8000 is in use`: stop the manually started Xarta process and rerun the scenario.
- A dependency check fails: confirm the repository is open in its development container,
  then run `make services-check`.
- Jinja generation fails: inspect the `template-engine-jinja` devcontainer service and
  `.dev/runtime/scenarios/postal-local/application.log`.
- Package or station rendering fails: inspect `results.json` when present, the application
  log, extracted `manifest.json`, and the retained station cache.
- CUPS submission is uncertain: do not rerun automatically. Inspect CUPS using the stable
  job name and reconcile the physical output before authorizing a new generation.
