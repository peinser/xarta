# Postal Production Station

For development setup, the safe fake-printer flow, output locations, focused renderer
tests, and the guarded CUPS test, see
[`development/postal-local.md`](development/postal-local.md).

## Role and current status

`apps/postal-station` is a headless, embeddable Python station application.
It has no direct PostgreSQL access and no local workflow database. The server is the
authority for runs, attempts, scans, and recovery.

The current application implements a one-shot CLI, a fake-only development polling mode,
environment configuration, an HTTP client, active-run lookup, package download and
verification, traveller rendering, ordered PDF composition, durable submission
journaling, sequential CUPS submission, print-event reporting, and explicit scan
submission. It does not provide a user interface, run-claim operation in a UI, barcode
hardware integration, production daemon packaging/deployment, offline mode, or secure
cache cleanup scheduler. The API client can create a claim and retains its run token in
memory; process-restart token recovery is not yet provided.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `POSTAL_STATION_API_URL` | Yes | Absolute HTTP(S) server URL; use HTTPS in production |
| `POSTAL_STATION_ID` | Yes | Server-registered station identity |
| `POSTAL_STATION_PRINTER` | Yes | CUPS destination passed to `lp -d` |
| `POSTAL_STATION_CACHE_DIR` | No | Restricted temporary extraction directory; default `~/.cache/xarta-postal-station` |
| `POSTAL_STATION_API_TOKEN` | No in code | Bearer token; mandatory for production operation |
| `POSTAL_STATION_REQUEST_TIMEOUT_SECONDS` | No | Positive request timeout, default 30 |
| `POSTAL_STATION_MAX_PACKAGE_BYTES` | No | Positive download limit, default 1 GiB |
| `POSTAL_STATION_TIMEZONE` | No | IANA timezone used by mock handover dates; default `Europe/Brussels` |

The parser accepts plain HTTP and a missing token for development. That is not a
production security claim. Production requires TLS, authenticated station identity,
site-scoped authorization, and run-scoped access. The client sends a static station
bearer token plus an in-memory token for package download and acknowledgement. It does
not implement mutual TLS or durable run-token recovery.

## Expected server API

The client calls:

```text
POST /api/v1/postal/local/production-runs/claims
GET  /api/v1/postal/local/stations/{station_id}/active-runs
GET  /api/v1/postal/local/production-runs/{run_id}/package
POST /api/v1/postal/local/production-runs/{run_id}/package-acknowledgements
POST /api/v1/postal/local/print-jobs/{job_id}/attempts
POST /api/v1/postal/local/print-attempts/{attempt_id}/events
POST /api/v1/postal/local/production-scans
POST /api/v1/postal/local/handover-batches
```

Package downloads must return `X-Postal-Package-SHA256`,
`X-Postal-Manifest-SHA256`, and `X-Postal-Package-Bytes`. JSON responses are bounded to
1 MiB and package bytes to the configured maximum. A production server must authorize
every identifier against the authenticated station and site; possession of a run or job
ID must not grant access. Xarta implements these routes on the internal postal service;
there is no public ingress by default.

## Package verification and storage

Before acknowledging or printing, the station checks package byte count and SHA-256,
manifest SHA-256, exact checksum coverage, every member checksum, source size/digest/page
count, strict v3 manifest and traveller shapes, letter-only job kinds, contiguous source
and job sequence, UUIDs, print enums and boundary rules, and safe ZIP paths,
duplicate members, encryption, non-regular members, member count, and total uncompressed
size. It extracts with directory mode `0700` and file mode `0600`.

The verifier protects integrity only when receipt headers came from an authenticated
TLS server. SHA-256 metadata delivered by an unauthenticated peer is not authenticity.
The configured package-byte limit and verifier's separate 10,000-member/2 GiB expanded
limits must be sized for the station host.

`process_run` replaces only the authenticated extracted package. It retains rendered
letters and per-job journals under the protected run cache. It does not
securely erase flash/disk blocks, and completed cache directories remain until an
external policy removes them. Treat packages, PDFs, addresses, and retained traveller
sheets as confidential personal data. Restrict the OS account, cache path, backups,
spool, crash dumps, logs, and physical workstation; define retention and secure
destruction outside the current application.

## Printing workflow and ambiguity

Production-package v3 is the only accepted package schema and provides one globally
ordered `letter` job per mailpiece. The package contains only `manifest.json`,
`checksums.sha256`, and exact source PDFs. It never contains a printer name, credentials,
traveller PDF, merged content PDF, or letter PDF. For each job the station renders the
pinned traveller revision, merges sources in order, composes and retains one deterministic
atomic letter PDF, and submits it to the locally configured CUPS queue with the manifest's
color and sides settings,
so traveller and content cannot separate or interleave. In simplex output content starts
on page 2. In either duplex mode page 2 is intentionally blank and content starts recto
on page 3. The station handles the manifest jobs sequentially:

1. Strictly revalidate and render the source PDFs into the retained cache.
2. Ask the server to create an attempt with generation, rendered SHA-256, and byte count.
3. Journal `submission_started`, then invoke `lp -d <POSTAL_STATION_PRINTER>` without a shell.
4. Journal acceptance and report `completed`, `failed`, or `uncertain` to the server.
5. Continue only after the preceding job succeeds.

The current CUPS backend treats a successful `lp` process exit as station completion; it
does not poll the queue or printer for physical completion. A nonzero `lp` exit is
reported failed. Cancellation during the submission process is uncertain because CUPS
may have accepted the job. Other failures after CUPS accepted a job but before the
station received or reported success may also be operationally ambiguous even if the
client cannot classify them perfectly.

Do not retry an uncertain print attempt automatically. Inspect CUPS by the stable job
name, printed output, and traveller; then record a supervisor decision. A required
reprint must use a server-authorized new generation. Stable job names help correlation;
they are not printer idempotency keys, and CUPS can print duplicate submissions.

## Scanning and physical workflow

The operator or UI must explicitly choose `start_production`, `ready_for_handover`, or
`confirm_handover`; the station does not infer mode from current state. It sends station
ID, traveller barcode, and mode, verifies that the server acknowledged the same mode,
and only then invokes the UI's advance callback.

New travellers carry a machine-readable Code 128 barcode with the bare task UUID and a
human-readable bare UUID in the retained region. The detachable label contains neither.
Send the bare UUID when scanning; prefixed values are rejected.

The intended manual sequence is:

1. Scan `start_production` before handling the letter.
2. Cut at the dashed line and attach the detachable mailing label; preserve source order.
3. Apply every provider-neutral supply exactly as listed and verify the resolved provider,
   profile, policy, tariff, weight, unit prices, line amounts, and postage total snapshot.
4. Fold and insert the content, seal the envelope, retain the traveller outside it, and
   scan `ready_for_handover`.
5. Remove and retain the traveller before physical handover. Never put the internal
   Xarta barcode on the final envelope.
6. Complete the physical handover and scan `confirm_handover` against the correct batch.
7. Reconcile and destroy retained travellers according to site policy.

A scan timeout is ambiguous: the server may have committed it. Submit the same event and
let durable server deduplication answer; do not choose a different mode or advance the
physical workflow without acknowledgement. The server durably deduplicates by task,
mode, generation, and applicable handover batch.

## Recovery

On startup, `recover_active_runs()` requests server-authoritative runs for this station.
`resume_run()` redownloads the same run and skips package acknowledgement when the server
says it was already acknowledged. It does not reconstruct state from cache.

Redownload does not mean reprint. A `server_reported` journal is skipped; a locally
accepted submission is reported without resubmission. A `submission_started` record with
no accepted result becomes `uncertain` and fails closed. If local state is lost, the
server refuses new attempts for completed or uncertain jobs. Journals are a local safety
guard, not workflow authority; server run/job/attempt state remains authoritative.

If package verification fails, stop and preserve only the evidence allowed by incident
policy. If the API is unavailable, stop; offline production and deferred scan/event
upload are unsupported. If the printer result, traveller identity, generation, handover
batch, or server state conflicts, quarantine the item and escalate rather than guessing.

## Security checklist

- Place station APIs on a trusted network and require HTTPS and authenticated identity.
- Authorize station, site, run, job, attempt, and scan relationships server-side.
- Keep Bpost, Port Betaald, database, object-store, and tariff credentials off stations
  and out of packages.
- Allow-list the API host and expected printer; restrict who can change environment
  configuration.
- Run under a dedicated low-privilege OS account and protect the cache and CUPS spool.
- Log identifiers and transitions, not addresses, PDF contents, bearer tokens, or raw
  package data.
- Apply workstation patching, screen locking, physical controls, retention, and incident
  response procedures.

## Unsupported scope

The station does not support offline work, automatic reassignment, automatic reprint,
printer exactly-once guarantees, database access, carrier calls, franking decisions,
address correction, pricing, production planning, automated insertion, or e-MassPost
automation. It is not currently a standalone operator-ready application.
