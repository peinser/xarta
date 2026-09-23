"""Public documentation for agents using Xarta intake."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


bp = Blueprint(name="agents")


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#11231d">
  <meta name="description" content="Use Xarta from an AI agent to generate, sign, archive and deliver documents, with autonomous x402 payment when enabled.">
  <meta name="robots" content="index, follow">
  <link rel="alternate" type="text/markdown" href="/agents.md" title="Agent documentation">
  <title>Xarta for AI agents</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #11231d;
      --muted: #5b6964;
      --paper: #f4f1e9;
      --card: #fffdf7;
      --line: #d8d7cd;
      --signal: #d8ff54;
      --signal-dark: #355600;
      --terminal: #10211b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(rgba(17, 35, 29, .045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17, 35, 29, .045) 1px, transparent 1px),
        var(--paper);
      background-size: 32px 32px;
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.65;
    }
    a { color: inherit; }
    .shell { width: min(100% - 32px, 920px); margin: 0 auto; }
    header { padding: 28px 0; }
    .brand { display: inline-flex; align-items: center; font-size: 22px; font-weight: 800; letter-spacing: -.045em; text-decoration: none; }
    main { padding: 64px 0 96px; }
    .eyebrow { margin: 0 0 14px; color: var(--signal-dark); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
    h1 { max-width: 780px; margin: 0; font-size: clamp(43px, 8vw, 78px); line-height: .98; letter-spacing: -.065em; }
    .lede { max-width: 700px; margin: 28px 0 0; color: var(--muted); font-size: clamp(18px, 2.5vw, 23px); line-height: 1.5; }
    .autonomy { color: var(--ink); background: linear-gradient(transparent 62%, var(--signal) 62%); font-weight: 750; }
    .flow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; margin: 52px 0 80px; overflow: hidden; border: 1px solid var(--ink); border-radius: 16px; background: var(--ink); }
    .step { min-height: 154px; padding: 22px; background: var(--card); }
    .step span { display: block; color: var(--signal-dark); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; font-weight: 800; }
    .step strong { display: block; margin-top: 30px; font-size: 18px; line-height: 1.2; }
    section { margin-top: 72px; }
    h2 { margin: 0; font-size: clamp(28px, 4vw, 40px); line-height: 1.1; letter-spacing: -.04em; }
    .intro { max-width: 690px; margin: 14px 0 24px; color: var(--muted); font-size: 17px; }
    pre { margin: 0; overflow-x: auto; border: 1px solid #294039; border-radius: 16px; background: var(--terminal); color: #e9f1e4; padding: 24px; font: 13px/1.7 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; box-shadow: 0 18px 50px rgba(17, 35, 29, .14); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    :not(pre) > code { border: 1px solid var(--line); border-radius: 5px; background: rgba(255,255,255,.55); padding: 1px 5px; font-size: .88em; }
    .comment { color: #9db1a7; }
    .header { color: var(--signal); }
    .facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
    .fact { border-top: 3px solid var(--ink); padding-top: 18px; }
    .fact h3 { margin: 0 0 8px; font-size: 17px; }
    .fact p { margin: 0; color: var(--muted); font-size: 15px; }
    .note { margin-top: 24px; border: 1px solid #a9bb70; border-radius: 14px; background: #f2f8d9; padding: 18px 20px; color: #314020; }
    footer { border-top: 1px solid var(--line); padding: 28px 0 48px; color: var(--muted); font-size: 13px; }
    @media (max-width: 720px) {
      main { padding-top: 38px; }
      .flow { grid-template-columns: 1fr 1fr; margin-bottom: 60px; }
      .facts { grid-template-columns: 1fr; gap: 30px; }
    }
    @media (max-width: 430px) {
      .flow { grid-template-columns: 1fr; }
      .step { min-height: 120px; }
      .step strong { margin-top: 18px; }
      pre { margin-inline: -8px; padding: 18px; border-radius: 12px; }
    }
  </style>
</head>
<body>
  <header class="shell">
    <a class="brand" href="/agents" aria-label="Xarta agents home">\u03a7arta</a>
  </header>
  <main class="shell">
    <p class="eyebrow">Built for software, not dashboards</p>
    <h1>Document workflows an agent can finish.</h1>
    <p class="lede">Xarta turns a declared document flow into real work: generate, sign, archive and deliver. An agent can discover the contract, submit the flow and, <span class="autonomy">when x402 is enabled, pay for it autonomously.</span></p>

    <div class="flow" aria-label="Agent workflow">
      <div class="step"><span>01 / DISCOVER</span><strong>Inspect deployed capabilities</strong></div>
      <div class="step"><span>02 / CONSTRUCT</span><strong>Use a profile or build a DAG</strong></div>
      <div class="step"><span>03 / SETTLE</span><strong>Authorize the x402 payment</strong></div>
      <div class="step"><span>04 / EXECUTE</span><strong>Xarta accepts the workflow</strong></div>
    </div>

    <section>
      <p class="eyebrow">Model Context Protocol</p>
      <h2>Add Xarta as an MCP server</h2>
      <p class="intro">Connect an MCP client to the Streamable HTTP endpoint. The server consults document types, discovers executable graph contracts, validates and prices flows, and retrieves bounded archive results by immutable version.</p>
      <pre><code>/api/mcp

<span class="comment"># Read-only discovery and preparation</span>
list_document_types    get_document_type        check_document_type
get_capabilities       list_flow_profiles       get_flow_profile
prepare_flow           prepare_profile          get_archived_document
list_archived_document_versions

<span class="comment"># May execute irreversible document delivery</span>
submit_flow            submit_profile</code></pre>
      <div class="note"><strong>Profiles are optional.</strong> An agent may select an exact server-owned profile, or submit its own complete DAG using only the capabilities currently advertised by <code>get_capabilities</code>.</div>
    </section>

    <section>
      <p class="eyebrow">One stable contract</p>
      <h2>Or use HTTP directly</h2>
      <p class="intro">Discover deployed capabilities first. Profiles expose typed business inputs without revealing internal provider configuration, while generic intake accepts a complete caller-authored graph.</p>
      <pre><code><span class="comment"># Discover graph contracts, pricing state and optional profiles</span>
GET /api/v1/intake/capabilities
GET /api/v1/intake/profiles
GET /api/v1/intake/profiles/{name}/{version}

<span class="comment"># Validate and quote either mode without executing it</span>
POST /api/v1/intake/prepare
{ "id": "...", "dag": { "kind": "..." } }

<span class="comment"># Submit one exact profile</span>
POST /api/v1/intake/profiles/{name}/{version}
Content-Type: application/json

{
  "id": "10000000-0000-0000-0000-000000000001",
  "correlation_id": "20000000-0000-0000-0000-000000000001",
  "inputs": { "document": { "source": "generate", "id": "..." } }
}

<span class="comment"># Or submit a complete custom flow</span>
POST /api/v1/intake/
{ "id": "...", "dag": { "kind": "generate", "documents": [], "on": {} } }</code></pre>
    </section>

    <section>
      <p class="eyebrow">Native agent payment</p>
      <h2>The 402 response is the checkout</h2>
      <p class="intro">For a priced flow, Xarta returns official x402 v2 payment requirements. A compatible agent or client authorizes those exact terms and retries the same request. There is no browser checkout or human payment link in this path.</p>
      <pre><code><span class="comment"># Initial response when payment is required</span>
HTTP/1.1 402 Payment Required
<span class="header">PAYMENT-REQUIRED: &lt;x402-v2-requirements&gt;</span>
Content-Type: application/json

{"error":"Payment is required for this intake."}

<span class="comment"># Retry the identical method, URL and body</span>
POST /api/v1/intake/profiles/{name}/{version}
<span class="header">PAYMENT-SIGNATURE: &lt;x402-v2-payment-payload&gt;</span>
Content-Type: application/json

<span class="comment"># Settlement succeeded and the workflow was accepted</span>
HTTP/1.1 200 OK
<span class="header">PAYMENT-RESPONSE: &lt;x402-v2-settlement-response&gt;</span>

{"id":"...","delivery_profile":"...","payment":{"transaction":"..."}}</code></pre>
      <div class="note"><strong>If x402 is not enabled:</strong> the same intake contract remains available under the deployment's normal access policy and successful asynchronous acceptance returns <code>202 Accepted</code>.</div>
    </section>

    <section>
      <p class="eyebrow">Operational semantics</p>
      <h2>Three things an agent should know</h2>
      <div class="facts">
        <article class="fact"><h3>Use a stable flow ID.</h3><p>Paid intake requires an explicit UUID. Retry the identical request with the same ID so payment terms and execution identities remain stable.</p></article>
        <article class="fact"><h3>Settlement comes first.</h3><p>Xarta verifies and settles through the configured facilitator before it publishes work. A rejected payment does not start the workflow.</p></article>
        <article class="fact"><h3>Acceptance is asynchronous.</h3><p>A successful intake response means the root task was accepted for execution. Completion and provider outcomes remain part of the declared flow.</p></article>
      </div>
    </section>
  </main>
  <footer><div class="shell">\u03a7arta &middot; asynchronous document generation and workflow services</div></footer>
</body>
</html>
"""

MARKDOWN = """# \u03a7arta for AI agents

Xarta constructs and executes document workflows. Use the MCP endpoint at `/api/mcp`
or the HTTP intake API directly.

## MCP tools

Read-only tools:

- `list_document_types`: deployed document types and public rendering defaults
- `get_document_type`: one document type's current template configuration and defaults
- `check_document_type`: generic render-request validation and default resolution without rendering
- `get_capabilities`: complete recursive flow schema, node explanations, examples, outcomes, and pricing state
- `list_flow_profiles`: available exact server-owned policies
- `get_flow_profile`: one exact profile and its typed inputs
- `prepare_flow`: validate, normalize, and quote a custom DAG without execution
- `prepare_profile`: compile, validate, and quote a profile without execution
- `get_archived_document`: exact metadata and a bounded immutable-version resource link
- `list_archived_document_versions`: cursor-paginated immutable version history

Execution tools:

- `submit_flow`: submit a complete caller-authored DAG
- `submit_profile`: submit an exact profile with typed inputs

Profiles are optional. For a custom graph, call `get_capabilities`, compose only
advertised node kinds through their `on` outcome edges, assign an explicit flow UUID,
and call `prepare_flow` before submission.

Archive resources use
`xarta-archive://{archive}/documents/{document_id}/versions/{version_id}`. Resource
reads are size-bounded and verify persisted size, content type, and SHA-512. Public MCP
and archive exposure remains unsafe until caller authentication and tenant ownership are
implemented.

## x402 payment

When pricing is enabled, preparation returns the current selling price. An unpaid
submission returns status `402` and `payment_required`. Authorize those exact x402 v2
terms and retry the identical tool arguments with `payment_signature`. A settled
submission returns status `200`, `payment_response`, and transaction evidence.

When x402 is disabled, accepted asynchronous intake returns status `202` under the
deployment's normal access policy.

## Direct HTTP

```text
GET  /api/v1/intake/capabilities
GET  /api/v1/intake/profiles
GET  /api/v1/intake/profiles/{name}/{version}
POST /api/v1/intake/prepare
POST /api/v1/intake/
POST /api/v1/intake/profiles/{name}/{version}
```

Paid HTTP retries use `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and
`PAYMENT-RESPONSE`. A successful intake response means the root task was accepted;
workflow completion remains asynchronous.
"""


@bp.get("/agents")
async def agents(_: Request) -> HTTPResponse:
    return response.html(
        PAGE,
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@bp.get("/agents.md")
async def agents_markdown(_: Request) -> HTTPResponse:
    return response.text(
        MARKDOWN,
        content_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
