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

## Using the examples

Select a named example in your OpenAPI viewer's request or response dropdown.
Examples are embedded in `components.examples`, referenced by HTTP media types,
and also provided on reusable schemas so viewers do not need to invent
`additionalProp1` objects. Parameter and response-header examples show concrete
IDs, opaque cursors, idempotency keys, content negotiation, and payment wire formats.

The principal scenarios are:

| Scenario | Examples to start with |
| --- | --- |
| Render → sign → archive → email → notify | `InvoiceDeliveryWorkflow`, `RenderInvoice` |
| Existing document with delivery/failure branches | `EmailExistingDocument`, `PreparedInvoice` |
| Repository's exact profile and typed inputs | `ExactProfileWorkflow`, `ExactProfileInputs`, `FlowProfile` |
| Upload source bytes with a flow file | `MultipartFlowDefinition` plus the `submitFlow` upload command |
| PDF and UBL archive representations, versions and replay | `ArchiveSnapshotManifest`, `ArchiveVersion`, `ArchiveReplayed` |
| Transform and multipart webhook variants | `TransformMerge`, `TransformSplit`, `WebhookMultipart` and the node schema examples |
| ZIP job queued, processing or failed | `BundleQueued`, `BundleProcessing`, `BundleMissingSource` |
| Render-envelope validation | `DocumentCheckValid`, `DocumentCheckInvalid` |
| UBL invoice/credit note and validation results | `UblInvoice`, `UblCreditNote`, `UblBusinessValid`, `UblSchemaInvalid`, `UblUnavailable` |
| Peppol discovery including unknown status | `PeppolRegistered`, `PeppolNotRegistered`, `PeppolIndeterminate` |
| Provider delivery feedback and duplicate handling | `ResendDelivered`, `ResendBounced`, `EInvoiceSent`, `RecommandSent`, `AlreadyApplied` |
| Doccle provisioning and account linking | `EnsureReceiver`, `ReceiverPending`, `DoccleLinked`, `DoccleUnlinked` |
| Postal claim, package receipt, printing, scanning and handover | `PostalRunClaimed`, `PostalRunReplayed`, `PostalProductionRun`, `PostalPrintUncertain`, `PostalHandoverScan` |
| SEPA structured/unstructured remittance | `SepaStructuredPayment`, `SepaInvoicePayment` |

Related examples reuse synthetic invoice `INV-2026-0042`, customer `CUST-0042`,
and stable UUIDs. Their document type `invoice`, Jinja template, destinations,
signing policy, receiver subject fields, and providers must be configured in your
deployment. Document references must already exist unless an upstream node creates
them. The `development-standard@2` example matches the repository configuration;
the complete UBL XML examples are the vendored official billing examples and use
their own invoice identities. UBL result examples were obtained using Xarta's
offline validator, including its ruleset digest.

PDF/ZIP uploads and downloads, QR images, and multipart forms have file-oriented
`x-codeSamples`. Set `XARTA_URL` to your service origin (for example,
`http://localhost:8000`), save the named JSON example to the indicated file, and use
your actual document bytes. Archive `manifest` is a JSON **form field**, whereas
intake `flow` is a JSON **file part**; the commands show the difference. Production
station credentials are taken from environment variables, never embedded.

Provider events, package checksums, settlement evidence, and payment signatures are
illustrative unless stated otherwise. They are not live credentials, transferable
payment authorization, or a claim about a participant's live registration. Obtain
fresh payment terms/signatures, calculate checksums from your actual bytes, and sign
provider callbacks using the configured sandbox secret. No-body responses have no
fabricated payload; empty job details or empty discovery results denote real empty
states rather than undocumented fields.

## Editing and verification

1. Trace the handler, parser, serializer, and callers. Update the contract in the
   same change as routes, fields, validation, outcomes, authentication, media types,
   headers, status codes, pagination, idempotency, or callback behavior.
2. Edit HTTP operations and schemas directly in `v1/openapi.json`. Keep stable,
   unique `operationId` values, concrete schemas, examples, and actual error shapes.
   Do not advertise authentication or status codes that are not implemented.
   Add named media-type examples for JSON/XML requests and responses, schema examples,
   and parameter examples. Populate open-ended maps with realistic business data;
   include significant alternatives and error states. Reuse `components.examples`
   for shared payloads. Review descriptions, IDs and status semantics together.
3. `Workflow_*`, `RenderRequest`, `PreviewRequest`, and `DocumentTypeCheckRequest`
   schemas reuse executable protocol definitions.
   After changing those definitions, run:

   ```console
   uv run --frozen python tools/openapi_contract.py --update-schemas
   ```

   Curated schema examples survive a refresh. Review the generated diff and update
   examples for changed validation rules; refreshing does not update HTTP semantics.
   Deployment-specific profile input contracts remain discoverable through intake.
4. Update `info.version` and `v1/CHANGELOG.md`: patch for documentation corrections,
   minor for compatible additions, major for breaking contract changes. During the
   pre-release phase, replace obsolete behavior in `v1` directly and explicitly
   record breaking changes. After public release, an intentional breaking HTTP API
   change needs a new API directory/path major; preserve released contracts.
   Flow-profile versions remain independently immutable.
5. Run `make contracts-check` (also included in `make verify`). It checks reference
   resolution, schema validity, operation IDs, route coverage including aliases,
   equality with executable capability/render schemas, example coverage, schema
   conformance (including formats and JSON-encoded manifests), and absence of
   generated `additionalPropN` keys in example payloads. Example annotation edits
   do not weaken executable-schema drift detection. The check does not execute
   providers or prove business semantics; review parsers and serializers as well.

For independent OpenAPI structural validation, without adding a project dependency:

```console
uvx --from openapi-spec-validator openapi-spec-validator contracts/openapi/v1/openapi.json
```

Git history preserves contract revisions; do not copy the entire document into a
new directory for each patch or minor revision. Keep changes to a contract and its
implementation atomic.
