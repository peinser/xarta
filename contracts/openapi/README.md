# Xarta OpenAPI contracts

[v1/openapi.json](v1/openapi.json) is the OpenAPI **3.1.0** contract for Xarta's
HTTP APIs and the workflow capabilities accepted through intake. Open it in an
OpenAPI 3.1-compatible viewer or client generator. JSON is used so repository checks
can read it without adding a YAML dependency.

## Layout and scope

- `v1/openapi.json`: one self-contained contract, grouped by service tags, with
  reusable request, response, and recursive workflow schemas.
- `v1/CHANGELOG.md`: contract revisions and compatibility notes.
- `../../tools/openapi_contract.py`: schema refresh and offline consistency check
  (run from the repository root as shown below).

The directory version follows the HTTP API major version, not the Xarta package
version. `info.version` is the semantic version of the contract revision. The first
contract is `1.0.0`; it describes the existing `/api/v1` API and does not announce a
public software release.

This is the union of the independently deployable services, not a promise that
every endpoint is enabled on every installation. Set the server URL to the origin
of the relevant service or gateway. Deployment-specific ingress access controls
are outside this contract; operation-level security describes authentication
actually enforced by Xarta. Do not infer public accessibility from `security: []`.

The canonical paths use `/api/v1`. Service blueprints also expose floating
`/api/<service>` aliases, recorded in `x-path-alias`; prefer pinned paths for clients.
`/agents`, `/agents.md`, and process probes are unversioned. Worker-only capabilities
(`generate`, `debug`, `wait-for`, and workflow operations on other workers) are
represented by `Workflow_node_*` schemas under intake, not invented REST routes.
Each node includes its valid outcome transitions and runtime constraints.

NATS routing slips and MCP's JSON-RPC tools/resources are not REST APIs. Their
protocols remain documented in [tracked capabilities](../../docs/tracked-capabilities.md)
and the [MCP documentation](../../docs/mcp.md). MCP uses `/api/mcp` on its own
service; this OpenAPI document covers the underlying HTTP capabilities, not the
MCP transport. Provider callbacks are incoming paths. The workflow `webhook` node
supports caller-selected payloads and URLs; it does not imply a fixed outgoing
callback payload.

## Editing and verification

1. Trace the handler, parser, serializer, and callers. Update the contract in the
   same change as routes, fields, validation, outcomes, authentication, media types,
   headers, status codes, pagination, idempotency, or callback behavior.
2. Edit HTTP operations and schemas directly in `v1/openapi.json`. Keep stable,
   unique `operationId` values, concrete schemas, examples, and actual error shapes.
   Do not advertise authentication or status codes that are not implemented.
3. `Workflow_*`, `RenderRequest`, `PreviewRequest`, and `DocumentTypeCheckRequest`
   schemas reuse executable protocol definitions.
   After changing those definitions, run:

   ```console
   uv run --frozen python tools/openapi_contract.py --update-schemas
   ```

   Review the generated diff; refreshing schemas does not update HTTP semantics.
   Deployment-specific profile input contracts remain discoverable through intake.
4. Update `info.version` and `v1/CHANGELOG.md`: patch for documentation corrections,
   minor for compatible additions, major for breaking contract changes. During the
   pre-release phase, replace obsolete behavior in `v1` directly and explicitly
   record breaking changes. After public release, an intentional breaking HTTP API
   change needs a new API directory/path major; preserve released contracts.
   Flow-profile versions remain independently immutable.
5. Run `make contracts-check` (also included in `make verify`). It checks reference
   resolution, schema validity, operation IDs, route coverage including aliases,
   and equality with executable capability/render schemas. It does not prove that
   every response matches the specification; review handler semantics as well.

For independent OpenAPI structural validation, without adding a project dependency:

```console
uvx --from openapi-spec-validator openapi-spec-validator contracts/openapi/v1/openapi.json
```

Git history preserves contract revisions; do not copy the entire document into a
new directory for each patch or minor revision. Keep changes to a contract and its
implementation atomic.
