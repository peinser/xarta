# Flow Profiles

## Purpose

A flow profile is a named, exact-version, server-owned delivery policy that compiles to
Xarta's existing executable DAG. It prevents callers from copying shared delivery logic
without adding another execution engine. Workers, pricing, payment staging, and
capability services receive an ordinary compiled `DocumentFlowRequest`; they never
resolve profile names at runtime.

## Caller APIs

The preferred API accepts only identity and declared business inputs:

```http
POST /api/v1/intake/profiles/payroll-standard/7
Content-Type: application/json

{
  "id": "10000000-0000-0000-0000-000000000001",
  "correlation_id": "20000000-0000-0000-0000-000000000001",
  "inputs": {
    "document": {
      "source": "generate",
      "id": "90000000-0000-0000-0000-000000000001"
    },
    "employee": {"employee_id": "EMP-12345"}
  }
}
```

The generic intake endpoint supports the equivalent envelope:

```json
{
  "id": "10000000-0000-0000-0000-000000000001",
  "delivery_profile": "payroll-standard@7",
  "delivery_values": {
    "document": {
      "source": "generate",
      "id": "90000000-0000-0000-0000-000000000001"
    },
    "employee": {"employee_id": "EMP-12345"}
  }
}
```

Submission requires an exact positive integer version. There is no `current` execution
alias: retries, quotes, and payments must not silently change semantics when operators
advance a profile.

```text
GET /api/v1/intake/profiles
GET /api/v1/intake/profiles/{name}/{version}
```

These endpoints expose descriptions, input contracts, current-version metadata, and
semantic fingerprints. They do not expose DAGs, credentials, provider accounts, or
provider configuration.

## Configuration

```json
{
  "schema_version": 1,
  "profiles": {
    "payroll-standard": {
      "current_version": 7,
      "versions": {
        "7": {
          "description": "Archive payroll and notify the caller",
          "fingerprint": "sha256:<calculated-profile-fingerprint>",
          "inputs": {
            "document": {
              "type": "document-source",
              "allowed_sources": ["generate"]
            },
            "employee": {
              "type": "json-object",
              "maximum_bytes": 4096
            }
          },
          "dag": {
            "node_key": "archive-payroll",
            "kind": "archive",
            "destination": "payroll-evidence",
            "documents": [{"$input": "document"}],
            "on": {
              "created": [{
                "node_key": "notify-payroll",
                "kind": "webhook",
                "url": "https://payroll.example.test/xarta/completed",
                "data": {"$input": "employee"}
              }]
            }
          }
        }
      }
    }
  }
}
```

`current_version` is operator metadata and must identify a retained version. Published
versions are append-only. Changing graph shape, literals, input constraints,
destinations, or outcomes requires a new version.

Every version pins its calculated fingerprint. Generate it before review:

```bash
python -m xarta.flow_profiles fingerprint flow-profiles.json payroll-standard 7
python -m xarta.flow_profiles validate flow-profiles.json
```

Startup rejects a changed exact version and reports its name, version, configured
fingerprint, and newly calculated fingerprint. JSON formatting and object-key ordering do
not affect the calculation. A semantic change must be copied to a new version and given
that version's calculated fingerprint.

The repository keeps an append-only lock manifest mapping `name@version` to fingerprint.
`make verify` checks the profile file against that lock, and CI compares the lock with the
base revision: historical entries may neither change nor disappear. New entries may only
be appended for new versions. GitOps repositories should use the same pattern:

```bash
python -m xarta.flow_profiles validate-lock flow-profiles.json flow-profile-lock.json
python -m xarta.flow_profiles check-lock-history flow-profile-lock.json baseline-lock.json
```

Runtime loading repeats the declared-fingerprint check, so a mutated version cannot start
even if presubmit validation was bypassed. The history lock prevents changing both policy
and fingerprint under an existing exact version in reviewed Git history.

## Input Slots

An input slot is an object containing exactly one field:

```json
{"$input": "document"}
```

Compilation replaces the complete slot with one validated caller value. There is no
interpolation, JSON Pointer, property access, arithmetic, condition, environment lookup,
runtime result reference, or expression language.

Supported types are `string`, `string-list`, `uuid`, timezone-aware `instant`, bounded
`json-object`, `document-source`, `localized-strings`, and `doccle-receiver`. Constraints
include `maximum_length`, `maximum_items`, `maximum_bytes`, `allowed_sources`, and
`require_version`.

## Generated Values And Document IDs

Callers do not supply internal document IDs used to connect profile nodes. Profiles
declare named generated values:

```json
"generated_values": {
  "rendered_document_id": {"type": "uuid"}
}
```

They are referenced with a whole-value slot:

```json
{
  "source": "render",
  "id": {"$generated": "rendered_document_id"},
  "request": {"template": "payroll"}
}
```

A downstream node can use the same value:

```json
{
  "source": "generate",
  "id": {"$generated": "rendered_document_id"}
}
```

Generated UUIDs are deterministic from the flow ID, exact profile version, profile
fingerprint, and readable generated-value name. Named values are preferred to `$1`/`$2`: positional variables are
order-dependent and become unclear as policies evolve. Generated values exist at compile
time only. Provider document IDs and other runtime results remain durable tracking state
and are not interpolated into later DAG nodes.

Execution mechanics remain server-owned. Slots are rejected in `adapter`, `destination`,
`document_type`, `headers`, `method`, `sender`, `template_engine`, and `url`. Unknown,
missing, undeclared, and unused inputs fail before any side effect.

## Identity And Pricing

Every node requires a stable lowercase kebab-case `node_key`. Static node IDs are:

```text
UUIDv5(flow_id, "xarta:flow-profile:{name}@{version}:{profile_fingerprint}:node:{node_key}")
```

They remain stable across object-key formatting, but differ across flows, profile
versions, and any in-place policy mutation. Including the profile fingerprint prevents a
misconfigured edit of a published version from reusing one `Node.id` for changed
semantics. `node_key` is removed before execution.

Each profile has a domain-separated SHA-256 fingerprint over its name, version, typed
input contract, and canonical DAG. Each prepared intake has another fingerprint over the
flow ID, compiled DAG, and uploaded source bytes and metadata. Correlation IDs are tracing
data and do not alter executable semantics. Pricing and paid intake bind to the prepared
fingerprint, not merely the friendly profile name.

## Startup Validation

Intake reads `FLOW_PROFILES_CONFIG_PATH` once at startup and validates every retained
version:

- configuration and input schemas;
- exact slot syntax and declared/used inputs;
- unique node keys and forbidden dynamic mechanics;
- compilation through the normal DAG parser;
- availability of every compiled capability in `INTAKE_CAPABILITIES`.

Validation performs no network calls. A bad retained version prevents the new intake
replica from becoming ready.

## GitOps Deployment

The recommended production setup keeps profile JSON in the environment Git repository
and generates a content-addressed ConfigMap. With Kustomize:

```yaml
configMapGenerator:
  - name: xarta-flow-profiles
    files:
      - flow-profiles.json=policies/xarta/flow-profiles.json
generatorOptions:
  disableNameSuffixHash: false
```

Pass the generated name to Helm:

```yaml
services:
  intake:
    profiles:
      existingConfigMap: xarta-flow-profiles-<content-hash>
```

The ConfigMap must contain `flow-profiles.json`. The content-addressed name changes the
pod template and gives an auditable, atomic rollout. Argo CD or Flux owns review,
promotion, drift detection, and rollback.

For smaller deployments and CI, Helm can render the ConfigMap:

```yaml
services:
  intake:
    profiles:
      config:
        schema_version: 1
        profiles: {}
```

Do not use Helm `tpl` inside profile JSON. Templated policy obscures types and makes the
reviewed source differ from executed policy. Profiles contain no credentials; provider
credentials remain in provider-specific Secrets, so profiles use a ConfigMap.

Intake loads profiles only at startup. Projected changes are not dynamically reloaded;
a rollout is required, keeping one process bound to one registry for its lifetime.

## Rollout

1. Retain version 7 unchanged.
2. Add and review version 8.
3. Deploy the new ConfigMap and intake rollout.
4. Inspect the exposed version 8 contract and fingerprint.
5. Migrate callers to `/profiles/payroll-standard/8`.
6. Keep version 7 while retries, quotes, or dormant paid admissions reference it.
7. Remove version 7 only after its operational retention window closes.

## Non-Goals

Profiles do not add a CRUD API, database policy store, CRD, controller, dynamic reload,
loops, predicates, runtime expressions, or another workflow engine. A CRD can be
reconsidered if profiles later need Kubernetes status, admission webhooks, independent
resource ownership, or controller reconciliation. A ConfigMap is the clearer primitive
today.
