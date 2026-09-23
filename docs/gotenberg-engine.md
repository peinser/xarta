# Gotenberg rendering engine

[Gotenberg](https://gotenberg.dev) is registered in the Render service as the `gotenberg`
template engine kind, next to `jinja` and `scriptura`. Gotenberg performs no templating.
It converts a finished HTML or markdown document into a PDF through its Chromium routes,
so the render request carries the document itself rather than a template reference and a
payload to bind to it.

Use it when the caller already owns the document, or when an upstream step has produced
HTML or markdown that must become a PDF through Chromium instead of the converter built
into a templating engine.

| `payload.content_type` | Gotenberg route                     | Files sent            |
| ---------------------- | ----------------------------------- | --------------------- |
| `text/html`            | `/forms/chromium/convert/html`      | `index.html`          |
| `text/markdown`        | `/forms/chromium/convert/markdown`  | `index.html`, `index.md` |

## Request contract

The Render and Preview APIs accept the engine like any other:

```json
{
  "document_type": "invoice",
  "template_engine": "gotenberg",
  "content_type": "application/pdf",
  "metadata": {"title": "Invoice 2026-0001", "author": "Xarta"},
  "template_engine_options": {
    "gotenberg": {
      "kind": "gotenberg",
      "properties": {
        "paperWidth": "8.27",
        "paperHeight": "11.7",
        "marginTop": "0.5",
        "printBackground": true,
        "preferCssPageSize": true
      }
    }
  },
  "payload": {
    "content_type": "text/html",
    "data": "<html><body>Invoice</body></html>"
  }
}
```

- `payload.data` must be the document as a string, and `payload.content_type` selects the
  route. HTML is sent as `index.html`.
- `content_type` must be `application/pdf`. The Chromium conversion routes produce nothing
  else.
- `template_engine_options.<identifier>.properties` are passed verbatim as Gotenberg form
  fields, so every Chromium page property Gotenberg documents is available without a Xarta
  change. Booleans are sent as `true` or `false`. The block is optional, unlike the
  template path required by the templating engines.
- `metadata` is written into the PDF. The lowercase Xarta keys documented by the render
  request schema are translated to the capitalized names Gotenberg expects, and unknown
  keys are forwarded unchanged.
- `parameters` are ignored. They describe template variables, and Gotenberg has no
  template to bind them to.

## Markdown

Gotenberg's markdown route does not accept markdown on its own. It requires an
`index.html` entry point that includes the markdown through the `toHTML` template
function, which is also where a document gets its styling. Xarta sends the payload as
`index.md` and supplies this minimal entry point:

```html
<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{{ toHTML "index.md" }}</body></html>
```

Set `template_engine_options.<identifier>.wrapper` to replace it, which is how a markdown
render gets fonts, CSS, or a page header:

```json
{
  "template_engine": "gotenberg",
  "template_engine_options": {
    "gotenberg": {
      "kind": "gotenberg",
      "wrapper": "<html><head><style>body { font-family: serif; }</style></head><body>{{ toHTML \"index.md\" }}</body></html>"
    }
  },
  "payload": {"content_type": "text/markdown", "data": "# Invoice\n\nAmount due: 100 EUR"}
}
```

A wrapper that never references `index.md` is rejected with `400 Bad Request`. Gotenberg
would otherwise return a valid PDF containing none of the submitted markdown.

## Markdown

Gotenberg's markdown route does not accept markdown on its own. It requires an
`index.html` entry point that includes the markdown through the `toHTML` template
function, which is also where a document gets its styling. Xarta sends the payload as
`index.md` and supplies this minimal entry point:

```html
<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>{{ toHTML "index.md" }}</body></html>
```

Set `template_engine_options.<identifier>.wrapper` to replace it, which is how a markdown
render gets fonts, CSS, or a page header:

```json
{
  "template_engine": "gotenberg",
  "template_engine_options": {
    "gotenberg": {
      "kind": "gotenberg",
      "wrapper": "<html><head><style>body { font-family: serif; }</style></head><body>{{ toHTML \"index.md\" }}</body></html>"
    }
  },
  "payload": {"content_type": "text/markdown", "data": "# Invoice\n\nAmount due: 100 EUR"}
}
```

A wrapper that never references `index.md` is rejected with `400 Bad Request`. Gotenberg
would otherwise return a valid PDF containing none of the submitted markdown.

Requests that violate the payload or output content type are rejected by the Render API
with `400 Bad Request`. Gotenberg's own status code and body are returned to the caller
unchanged, so a rejected conversion surfaces its Chromium error message.

Every conversion carries a `Gotenberg-Trace` header holding the Xarta correlation
identifier, which ties Gotenberg's request logs to the originating render.

## Execution characteristics

- **Execution mode.** Synchronous. The final outcome is known when the HTTP response
  returns, so the engine creates no PostgreSQL execution, attempt, outcome, or tracked
  operation, exactly like the other template engines.
- **Idempotency scope.** None is negotiated with Gotenberg. Conversion is a pure function
  of the submitted documents, page properties, and metadata, so repeating a request
  produces an equivalent PDF rather than a duplicated side effect. Byte-level equality is not
  guaranteed, because the PDF carries generation timestamps.
- **Ambiguous side-effect boundary.** A timeout or a dropped connection leaves the
  conversion state unknown, but the only effect is a rendered document that is never read.
  No external record is created.
- **Duplicate delivery.** Safe. A duplicate request yields another equivalent PDF.
- **Retry safety.** Safe to retry. Gotenberg is stateless, and the caller supplies the
  complete input on every attempt.
- **Callbacks and reconciliation.** Not applicable. Gotenberg responds with the document
  and holds no state to reconcile.

## Configuration

Add the engine to the template engine configuration named by
`TEMPLATE_ENGINES_CONFIG_PATH`:

```json
{
  "gotenberg": {
    "endpoint": "http://template-engine-gotenberg:3000",
    "timeout": 30.0,
    "kind": "gotenberg"
  }
}
```

The timeout must accommodate Chromium startup and page load for the heaviest expected
document, including any `waitDelay` or `waitForExpression` page property.

In Helm, engines are declared under `services.render.templateEngines`, and each entry
resolves to `http://<release>-template-engine-<identifier>`. Gotenberg listens on 3000
rather than 80, so its entry sets `port`:

```yaml
services:
  render:
    templateEngines:
      - identifier: gotenberg
        kind: gotenberg
        timeout: 30.0
        port: 3000
```

The chart configures the Render service only. Deploying Gotenberg itself, along with its
Service named `<release>-template-engine-gotenberg`, is out of scope for this chart, as it
is for the other engines.

Locally, `.dev/compose.yml` runs Gotenberg as `template-engine-gotenberg` on the
`gotenberg-engine` network, overridable through `GOTENBERG_IMAGE`.

## Verification

Unit tests cover the request shaping. The wire contract itself is checked against a real
Gotenberg by the opt-in `gotenberg_contract` tests, which skip unless enabled:

```sh
docker compose -f .dev/compose.yml up -d template_engine_gotenberg
GOTENBERG_CONTRACT_ENABLED=true uv run --frozen pytest tests/protocol/test_gotenberg_contract.py
```

`GOTENBERG_CONTRACT_ENDPOINT` overrides the default `http://template-engine-gotenberg:3000`.
The conversion has no side effects, so the tests are safe to run against any instance.

## PDF/A and PDF/UA

Archival output needs no Xarta-specific configuration. `pdfa` and `pdfua` are ordinary
Gotenberg form fields, so the `properties` block already carries them:

```json
{
  "template_engine": "gotenberg",
  "content_type": "application/pdf",
  "template_engine_options": {
    "gotenberg": {
      "kind": "gotenberg",
      "properties": {"pdfa": "PDF/A-3b", "pdfua": true}
    }
  },
  "payload": {"content_type": "text/html", "data": "<html><body>Invoice</body></html>"}
}
```

Gotenberg accepts `PDF/A-1b`, `PDF/A-2b`, and `PDF/A-3b`. Anything else is rejected by
Gotenberg with `400` and a message listing the valid formats, which reaches the caller
unchanged. Converted output embeds a colour profile, so it runs roughly twice the size of
the same document without `pdfa`, and the levels above `PDF/A-1b` raise the PDF version
from 1.4 to 1.7.

Two caveats. Gotenberg warns that writing metadata usually breaks PDF/A compliance, and
this engine writes metadata whenever the request or the document type carries any, so
strict archival output wants an empty `metadata` and no document-type default metadata.
Xarta asserts that the requested format was applied, not that the result validates: run a
validator such as veraPDF if compliance has to be proven.

## Limitations

- Only the Chromium HTML and markdown conversion routes are wired up. The URL, screenshot,
  and LibreOffice routes are not exposed, because each needs an input shape the
  single-payload render request does not carry. The PDF engines are reached only through
  the `pdfa` and `pdfua` properties above, not as standalone merge or convert routes.
- One markdown document is sent per request, as `index.md`, even though Gotenberg accepts
  several `.md` files behind one wrapper.
- Assets are not uploaded. Documents must be self-contained, or reference resources
  Gotenberg's Chromium can reach over the network. This is why a wrapper carries its CSS
  inline.
