# Xarta

Asynchronous document workflow service. Clients submit a document flow as a DAG, or typed inputs for an exact flow-profile version; intake validates it and publishes a deterministic root task to NATS JetStream. Independently deployable capability workers execute the nodes by routing slip choreography: every task carries the remaining graph, and each worker publishes the successors for its outcome. PostgreSQL only tracks operations that stay pending after a worker returns.

- **Services**: Python 3.13 + Sanic + nats-py, managed with `uv`. Database changes go through Sqitch in `db/`; deployment through the Helm charts in `k8s/helm/`.
- **Workflow**: [CONTRIBUTING.md](CONTRIBUTING.md) defines the development environment, `make verify`, and the workflow execution rules workers must follow.

Write this code for human maintainers, not for minimum line count.

## API contracts

- The versioned OpenAPI contracts live in `contracts/openapi/`; start with its
  `README.md`. `contracts/openapi/v1/openapi.json` describes HTTP API v1 and all
  workflow capabilities accepted by intake.
- Every code change that changes a contract must update the corresponding OpenAPI
  contract in the same change. This includes routes, request/response fields,
  validation, capability outcomes, status codes, headers, media types,
  authentication, callbacks, pagination, and idempotency behavior. Document the
  implemented behavior, including deployment-dependent requirements.
- Maintain thorough, representative examples with every contract change. Every
  request/response JSON or XML media type needs named OpenAPI examples; every
  reusable schema and request parameter needs concrete examples. Use realistic
  synthetic domain data and consistent IDs across related requests, responses,
  workflow nodes, and callbacks. Populate free-form maps with meaningful keys and
  values, never `additionalProp1`, `string`, or empty-object filler. Empty objects
  and arrays are appropriate only when they represent an actual empty state.
- Cover meaningful alternatives: complete nonempty workflows and outcome branches,
  profile inputs, validation failures, pending/completed states, pagination cursors,
  idempotent replays, and provider feedback. Check examples against parsers and
  serializers, not just schemas. Label configuration prerequisites and illustrative
  cryptographic values; never include real secrets or personal data. For binary and
  multipart operations, provide usable file-oriented `x-codeSamples` and concrete
  JSON manifests instead of pretending filenames or placeholder text are file bytes.
- Reuse executable DAG/render schemas: run
  `uv run --frozen python tools/openapi_contract.py --update-schemas` when those
  definitions change, then review and update HTTP semantics manually. The refresh
  preserves curated schema examples; update them when validation rules change.
- Bump the contract's `info.version` appropriately and record compatibility notes
  in its `CHANGELOG.md`. Follow the pre-release/released versioning policy in
  `contracts/openapi/README.md`; flow-profile versions remain independently immutable.
- Run `make contracts-check` before considering a contract change complete; it is
  also part of `make verify`. Keep operation IDs stable unless intentionally
  changing the client-facing contract.

### API shape and consistency

- Prefer resource-oriented JSON over a flat export of database columns. Within a
  resource object, use `id` for its identity; group related identity and relationship
  fields, for example `version: {id, parent}` rather than sibling `version_id` and
  `parent_version_id`. A representation object should likewise identify itself as
  `id`, not repeat its enclosing resource name in every property.
- Nest fields when they describe one coherent concept, such as version identity,
  representation selection, or content properties. Keep nesting shallow; do not add
  wrapper objects merely to shorten a single field name or mirror internal classes.
- Give the same concept consistent names, types, and structure across requests,
  responses, detail views, and collections. Distinguish scalar references from
  embedded objects explicitly: `version.parent` is a UUID or null, not a recursive
  version object. Document intentional differences between creation inputs,
  references, and resolved responses rather than making callers guess.
- Make ownership and semantics clear: document/version metadata and representation
  metadata are distinct; content availability is distinct from workflow progress.
  Use specific names for states and timestamps, and document their scope and units.
- Prefer concise write acknowledgements containing the outcome and resolved
  resource identities. If returning a resource instead, return its consistent,
  documented representation; avoid arbitrary partial snapshots that mix an
  acknowledgement with selected detail fields. Expose internal replay, deduplication,
  or storage bookkeeping only when callers have a concrete need for it.
- Reuse established collection/pagination and error shapes. Separate stable
  machine-readable error codes from human-readable messages; do not overload one
  field with resource objects in one response and bare IDs in another.
- Apply these preferences to new or explicitly revised contracts. Trace all
  producers and consumers when changing a shape, and update serializers, parsers,
  OpenAPI schemas, examples, and documentation together under the versioning policy.

## Safety

- Before the first published release, remove or replace obsolete APIs, schemas,
  migrations, configuration, and code directly. Do not add backward compatibility
  unless the user explicitly requests it. After release, preserve compatibility for
  shipped behavior and persisted data unless a migration or breaking change is explicit.
- Keep changes scoped and preserve existing public behavior unless the task
  explicitly changes it.
- Never commit credentials, tokens, private keys, or environment-specific
  secrets.

## Important

You are a lazy senior HUMAN engineer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:

Does this need to be built at all? (YAGNI)
Does it already exist in this codebase? Reuse the helper, util, or pattern that's already here, don't re-write it.
Does the standard library already do this? Use it.
Does a native platform feature cover it? Use it.
Does an already-installed dependency solve it? Use it.
Can this be one line? Make it one line.
Only then: write the minimum code that works.
The ladder runs after you understand the problem, not instead of it: read the task and the code it touches, trace the real flow end to end, then climb.

Bug fix = root cause, not symptom: a report names a symptom. Grep every caller of the function you touch and fix the shared function once — one guard there is a smaller diff than one per caller, and patching only the path the ticket names leaves a sibling caller still broken.

Rules:

No abstractions that weren't explicitly requested.
No new dependency if it can be avoided.
No boilerplate nobody asked for.
Deletion over addition. Boring over clever. Fewest files possible.
Shortest working diff wins, but only once you understand the problem. The smallest change in the wrong place isn't lazy, it's a second bug.
Question complex requests: "Do you actually need X, or does Y cover it?"
Pick the edge-case-correct option when two stdlib approaches are the same size, lazy means less code, not the flimsier algorithm.
Mark deliberate simplifications that cut a real corner with a known ceiling (global lock, O(n²) scan, naive heuristic) with a ponytail: comment naming the ceiling and upgrade path.
Not lazy about: understanding the problem (read it fully and trace the real flow before picking a rung, a small diff you don't understand is just laziness dressed up as efficiency), input validation at trust boundaries, error handling that prevents data loss, security, accessibility, the calibration real hardware needs (the platform is never the spec ideal, a clock drifts, a sensor reads off), anything explicitly requested. Lazy code without its check is unfinished: non-trivial logic leaves ONE runnable check behind, the smallest thing that fails if the logic breaks (an assert-based demo/self-check or one small test file; no frameworks, no fixtures). Trivial one-liners need no test.

- Write code for human maintainers. This is critical. Do not use weird AI constructions. Be explicit.
- Prefer the direct standard-library or dependency API. Do not add round-trip
  canonicalization, manual encoding corrections, duplicate validation, or defensive
  branches unless a concrete security, interoperability, or input requirement needs
  them and is covered by a test.
- Avoid speculative abstractions and wrapper layers. Every helper, validation branch,
  and data structure must simplify current call sites or satisfy a stated requirement;
  do not validate internal constants on every request.
- Shared and domain-level validators must raise domain exceptions such as `ValueError`,
  not HTTP or transport exceptions. Translate errors once at the explicit transport
  boundary; do not catch an exception merely to re-raise the same validation failure as
  another type within domain or protocol code.
- Apply best coding practices.
- Be performance-minded.
- Implement the code with rigor, and be thorough in the architecture design.
- Tests should only be writen once we finalised a particular implementation. You should ask me when you think are done.
