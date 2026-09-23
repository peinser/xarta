# PDF signing

## Policy selection

Callers may set a stable signing policy on a signature node:

```json
{
  "kind": "signature",
  "policy": "xarta-dev-seal-v1",
  "documents": []
}
```

When `policy` is omitted, intake binds the `default_policy` from the shared
signature configuration before it creates the durable task. This prevents a
later deployment-default change from changing the meaning of a retry. The
signature worker resolves an omitted policy only as a compatibility fallback
for old tasks and direct development HTTP calls.

Development configures:

```text
SIGNATURE_CONFIG_PATH=.dev/conf/signature.json
SIGNATURE_ALLOW_DEVELOPMENT_POLICIES=true
```

The JSON configuration is loaded by both intake and signature. It contains the
default, immutable policy definitions, and signer identity/provider references.
Private-key paths, credentials, and provider secrets remain deployment
configuration and must not be stored in this file or in the DAG.

Resolution follows these rules:

1. An explicit known and permitted policy is retained unchanged.
2. An omitted policy is replaced by the configured default during intake.
3. An explicit unknown policy is rejected; it never falls back to the default.
4. A development-only policy is rejected when development policies are disabled.
5. The worker resolves an unbound old task only as a compatibility fallback.

Intake performs resolution before calculating the semantic fingerprint and
before serializing the root `NodeTask`. Successor signature nodes are bound in
the same recursive pass. Changing the deployment default therefore affects new
intake only and does not reinterpret an already prepared task.

Set `SIGNATURE_ALLOW_DEVELOPMENT_POLICIES=false` in deployments that must reject
all policies marked as development-only. Intake and signature startup fail if
their configured default is unknown or forbidden.

## Available policies

The development catalog currently exposes three executable policies.

| Policy | PDF profile | Subfilter | Digest | RFC 3161 timestamp | DSS/revocation | LTA document timestamp | Development only |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `xarta-dev-seal-v1` | Legacy PDF CMS | `/adbe.pkcs7.detached` | pyHanko legacy default | No | No | No | Yes |
| `pades-b-b-v1` | PAdES Baseline B-B | `/ETSI.CAdES.detached` | SHA-256 | No | No | No | Yes |
| `pades-b-t-v1` | PAdES Baseline B-T | `/ETSI.CAdES.detached` | SHA-256 | Required and validated | No | No | Yes |

### `xarta-dev-seal-v1`

Purpose: preserve Xarta's original local PDF signing behavior for development,
backwards compatibility, and comparison with the PAdES implementation.

- Profile: `legacy-pdf-cms`.
- Signer identity: `local-dev-v1` in the development catalog.
- PDF subfilter: pyHanko's legacy `/adbe.pkcs7.detached` default.
- Digest: not specified by the policy; pyHanko selects its legacy default.
- Certificate chain: configured intermediates are embedded, but no certificate
  is made trusted merely by embedding it.
- Timestamping: none.
- Validation evidence: no PDF DSS, OCSP response, or CRL is requested.
- Archival preservation: no LTA document timestamp is created.
- Assurance: not PAdES and not suitable as a production assurance claim.
- Scenario: `make scenario-signature-xarta-dev-seal-v1`.

### `pades-b-b-v1`

Purpose: create an actual PAdES Baseline B-B PDF signature for development and
integration testing.

- Profile: `pades-b-b`.
- Signer identity: `local-dev-v1` in the development catalog.
- PDF subfilter: explicitly `/ETSI.CAdES.detached` through
  `SigSeedSubFilter.PADES`.
- Digest: SHA-256, validated as part of policy configuration and existing-output
  matching.
- CMS attributes: pyHanko's PAdES signing path includes
  `signing_certificate_v2`.
- Certificate chain: configured intermediates are embedded; self-signed roots
  are rejected from the chain bundle and are not embedded.
- Timestamping: none. B-B does not include an RFC 3161 signature timestamp.
- Validation evidence: no PDF DSS, OCSP response, or CRL is requested.
- Archival preservation: no LTA document timestamp is created.
- Assurance: a technical B-B signature, currently marked development-only and
  not independently certified for production conformance.
- Scenario: `make scenario-signature-pades-b-b-v1`.

### `pades-b-t-v1`

Purpose: add a trusted RFC 3161 signature timestamp to the B-B signature for
development and integration testing.

- Profile: `pades-b-t`.
- Signer identity: `local-dev-v1` in the development catalog.
- PDF subfilter: `/ETSI.CAdES.detached`.
- Digest: SHA-256 for the document signature and timestamp request.
- Timestamping: pyHanko sends the CMS signature imprint to the deployment-bound
  RFC 3161 provider referenced by the policy. The response must contain a valid
  token issued by the exact configured TSA leaf and chaining to the explicit TSA
  trust root with network fetching disabled.
- Validation evidence: no PDF DSS, OCSP response, or CRL is embedded.
- Archival preservation: no LTA document timestamp is created.
- Failure behavior: timeout, transport, malformed-response, untrusted-token, and
  wrong-TSA failures abort signing. The service never returns a B-B signature as
  a B-T result.
- Assurance: a technical B-T signature, currently marked development-only and
  not independently certified for production conformance.
- Scenario: `make scenario-signature-pades-b-t-v1`.

The PAdES implementations either create and validate the requested profile or
fail. They do not silently return a legacy or lower-baseline signature.

## Planned policies

The profile enum reserves B-LT and B-LTA concepts, but the signature service
rejects catalogs containing these profiles until their dependencies are
implemented. They are not available signing policies today.

| Planned policy | Required behavior before registration |
| --- | --- |
| `pades-b-lt-v1` | B-T plus explicit trust, controlled OCSP/CRL retrieval, and embedded DSS evidence that validates offline |
| `pades-b-lta-v1` | B-LT plus an initial document timestamp and a separately implemented preservation lifecycle |

`xarta-dev-seal-v1` is not a PAdES policy. It does not promise trusted
timestamping, revocation evidence, long-term validation, an advanced electronic
seal, or a qualified electronic seal. More generally, a PAdES profile describes
a technical PDF signature format; it does not by itself establish an eIDAS
legal assurance level.

The production PAdES conformance target is ETSI EN 319 142-1 V1.2.1
(2024-01). pyHanko validation proves cryptographic integrity and the tested
certificate path, but is not by itself an independent structural conformance
assessment against every ETSI requirement.

## Configuration catalog

The development catalog is `.dev/conf/signature.json`. Its structure is:

```json
{
  "schema_version": 1,
  "default_policy": "xarta-dev-seal-v1",
  "signer_identities": {
    "local-dev-v1": {
      "provider": "local-pem"
    }
  },
  "policies": {
    "xarta-dev-seal-v1": {
      "profile": "legacy-pdf-cms",
      "signer_identity": "local-dev-v1",
      "development_only": true
    },
    "pades-b-b-v1": {
      "profile": "pades-b-b",
      "signer_identity": "local-dev-v1",
      "digest_algorithm": "sha256",
      "development_only": true
    },
    "pades-b-t-v1": {
      "profile": "pades-b-t",
      "signer_identity": "local-dev-v1",
      "digest_algorithm": "sha256",
      "timestamp_provider": "local-dev-tsa",
      "development_only": true
    }
  }
}
```

The parser rejects unknown fields, malformed identifiers, unknown signer
identities, unsupported digests, unavailable defaults, and profile combinations
missing required timestamp or trust references. The signature service
additionally rejects configured profiles it cannot execute.

Policy IDs and signer identity IDs have immutable meanings. Never repoint a
production ID to new cryptographic semantics. The current local provider accepts
one signer identity per process, so overlapping old/new private-key rotation is
not implemented yet. Drain pinned development tasks before replacing that local
identity. Production rotation requires a signer registry that can retain old
public identities while selecting the immutable identity referenced by each
policy.

## Timestamp provider configuration

The signature service resolves stable policy references from
`TIMESTAMP_PROVIDERS_CONFIG_PATH`. Callers and DAG definitions cannot supply a
TSA endpoint. The development catalog is `.dev/conf/timestamp-providers.json`:

```json
{
  "providers": {
    "local-dev-tsa": {
      "provider": "http-rfc3161",
      "endpoint": "http://127.0.0.1:8403/",
      "timeout_seconds": 5,
      "certificate_pem": ".dev/runtime/certificates/tsa-public.pem",
      "trust_root_pem": ".dev/runtime/certificates/root.pem"
    }
  }
}
```

The RFC 3161 adapter is unauthenticated HTTP(S). HTTPS is required except for a
loopback development endpoint. The timeout is mandatory and bounded to 60
seconds. The TSA leaf must be current, non-CA, usable for signing, and have an
exclusive critical `timeStamping` extended key usage. Its configured chain must
reach an explicit trust root. Optional `chain_pem` supplies TSA intermediates;
network certificate and revocation fetching remains disabled.

Production deployments can supply `services.signature.timestampProviders` as
an inline ConfigMap or an existing ConfigMap. It must contain
`timestamp-providers.json` and the public PEM files referenced from that JSON.
Set `existingConfigMapChecksum` when fixed-name external ConfigMap content
rotates so the signature pods restart with the new trust material.
Provider identifiers and their trust semantics are immutable policy
dependencies; create a new identifier when rotating endpoints or certificates.

## Signer configuration

The `local` signature adapter creates an approval signature with pyHanko and a
PEM-encoded private key plus X.509 certificate. The key and certificate must
match. Key material is loaded and validated once during service startup; the
parsed private key remains in process memory until shutdown.

`DOCUMENT_SIGN_CHAIN_PEM` optionally identifies a PEM bundle of intermediate
certificates. The signer embeds intermediates through pyHanko's certificate
store with root embedding disabled. Supplying a certificate for embedding does
not make it a trust anchor.

The `xarta-dev-seal-v1` branch intentionally retains pyHanko's legacy defaults:
it does not select the PAdES subfilter, request an RFC 3161 timestamp, embed DSS
validation material, or enable LTA document timestamping.

The local provider uses these deployment variables:

| Variable | Consumer | Purpose |
| --- | --- | --- |
| `SIGNATURE_CONFIG_PATH` | Intake and signature | Shared policy catalog path |
| `TIMESTAMP_PROVIDERS_CONFIG_PATH` | Signature | Deployment-bound RFC 3161 endpoint and trust catalog path |
| `SIGNATURE_ALLOW_DEVELOPMENT_POLICIES` | Intake and signature | Explicit development-policy authorization |
| `SIGNATURE_ADAPTER` | Signature | Compatibility adapter selector; currently `local` |
| `DOCUMENT_SIGN_PRIVATE_PEM` | Signature | Unencrypted development private-key path |
| `DOCUMENT_SIGN_PUBLIC_PEM` | Signature | Leaf signing-certificate path |
| `DOCUMENT_SIGN_CHAIN_PEM` | Signature | Optional intermediate-certificate PEM bundle |
| `SIGNATURE_DEFAULT_POLICY` | Intake and signature | Temporary compatibility override for the catalog default |

The local signer validates key/certificate matching, rejects CA leaf
certificates, rejects duplicate or embedded leaf certificates in the chain,
cryptographically verifies intermediate issuance, checks CA constraints and key
usage, and rejects roots in the intermediate bundle. PEM-in-process key custody
is intended for development and test environments, not as the final production
HSM or remote-QTSP architecture.

`make certificates` creates the development hierarchy under
`.dev/runtime/certificates`:

```text
root.pem       development trust anchor
chain.pem      intermediate signing CA
public.pem     document seal leaf certificate
private.pem    document seal private key
tsa-public.pem RFC 3161 signing leaf certificate
tsa-private.pem RFC 3161 signing private key
```

## Policy scenarios

Every policy in `.dev/conf/signature.json` must have a scenario definition. A
unit guard compares the catalog with the scenario registry so policy additions
cannot omit this coverage.

Run one policy:

```sh
make scenario-signature-xarta-dev-seal-v1
make scenario-signature-pades-b-b-v1
make scenario-signature-pades-b-t-v1
```

Run all configured signing policies sequentially:

```sh
make scenario-generate-sign-archive
```

Each scenario performs 50 generate/sign/archive flows and verifies:

- the explicit policy survives intake and durable execution;
- the expected PDF subfilter and digest are present;
- the signature is intact, cryptographically valid, and trusted from
  `root.pem` with network fetching disabled;
- `chain.pem` is embedded and `root.pem` is not;
- no timestamp, DSS, or LTA evidence appears for policies that prohibit it;
- B-T contains a valid and trusted RFC 3161 token from the configured TSA;
- the archived artifact remains `application/pdf`; and
- application logs contain every synchronous stage without errors.

Reports and application logs are written below:

```text
.dev/runtime/scenarios/signature/xarta-dev-seal-v1/
.dev/runtime/scenarios/signature/pades-b-b-v1/
.dev/runtime/scenarios/signature/pades-b-t-v1/
```

## Execution and concurrency

Signature nodes are synchronous DAG capabilities. JetStream owns delivery,
retries, acknowledgement, and deterministic task identity. The handler does not
create PostgreSQL workflow tracking state.

PDF parsing, hashing, CMS construction, private-key operations, and blocking TSA
HTTP requests run in a worker thread so they do not block the Sanic event loop
or JetStream heartbeat.
Documents in one node are processed sequentially to bound that node's CPU and
memory use. Concurrency across messages is controlled by `NATS_WORKERS` and
across pods by the deployment replica count.

## Idempotency and retries

The output document UUID is the idempotency key. Temporary storage is immutable
and create-only. A duplicate delivery reuses an existing output only after the
service verifies that:

- the signed PDF starts with the exact source PDF bytes, as expected for an
  incremental PDF signature;
- the latest embedded signature is intact and cryptographically valid;
- the signature covers the entire immutable file;
- its subfilter and digest match the pinned policy; and
- the signature certificate is the certificate configured for the signer; and
- B-T has a valid, trusted timestamp from the exact configured TSA certificate,
  while B-B and legacy outputs have no signature timestamp.

This is an operational existing-output check, not a public PAdES validation or
conformance API. It intentionally answers whether an occupied immutable output
can be reused for the pinned task.

An occupied output UUID that fails these checks is an error and is never
overwritten. Concurrent deliveries converge on the immutable winner using the
same checks.

Document metadata is persisted before document bytes. If the binary write
fails, a retry can safely reuse the identical metadata and complete the binary
write. The remaining ambiguous boundary is after the signed output has been
persisted but before successor publication or acknowledgement; duplicate
delivery resolves that boundary by validating and reusing the persisted output.

Idempotency lasts as long as the generated output remains in temporary storage.
The adapter has no callback or reconciliation workflow. It is retry-safe within
that storage lifetime, but deterministic task identity does not make the PDF
signature bytes deterministic because signing time and some algorithms may vary.

## Performance

pyHanko's PDF and CMS implementation remains responsible for signature
construction. The local adapter implements pyHanko's documented raw-signing
extension point to retain the parsed cryptography private key instead of parsing
it for every dummy and real signature. Each policy scenario reports cold,
steady-state, and per-stage distributions.

Treat those timings as local measurements unless the environment and regression
threshold are explicitly fixed.
