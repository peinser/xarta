# Search indexing

The `search-index` DAG node declaratively indexes one immutable document source
version into a named destination. It performs no search queries and does not expose
a retrieval-augmented generation (RAG) API.

Search is deliberately independent from archive. Archiving a document never indexes
it automatically, and a searchable document does not have to be archived. Archive is
the authoritative content/version store; search destinations are disposable,
rebuildable projections selected only by explicit `search-index` nodes.

```json
{
  "kind": "search-index",
  "document": {
    "source": "archive",
    "archive": "payroll",
    "id": "93000000-0000-0000-0000-000000000001",
    "version": "0192f01e-bc41-7db1-a8ef-cc39a8d15421"
  },
  "destination": "documents",
  "on": {
    "indexed": [],
    "unchanged": [],
    "unsupported_content": [],
    "index_rejected": []
  }
}
```

`document` is singular by design. `source` and `id` are required. `version` should
identify an immutable source version when known. For an archive source it may be
omitted to retrieve the current version; the worker then records the concrete version
ID returned by archive details rather than indexing under the mutable label `latest`.
Archive projections trust archive's immutable concrete version identity and do not
download bytes or store a second content digest. For other sources without an
explicit version, the content digest is used as the immutable projection identity. A
repeated generic-source submission with the same SHA-256 digest emits `unchanged`;
reusing the identity with different bytes emits `index_rejected`. The node also
supports the common execution outcomes.

An archive-derived projection records `archive`, `document_id`, and the concrete
`version_id` as its source. UUIDs are retained as UUIDs inside the service and are
converted to strings only by database or search-backend transports. Archive names
are part of uniqueness; equal document and version UUIDs in two archives are
different sources. Projection revision remains part of projection identity.

Version and representation metadata are arbitrary JSON objects. An archive projection stores
the immutable version metadata, `default_representation_id`, and a namespaced descriptor for
every representation. It has no singular content type or digest. Re-indexing the same generic
source and digest may overwrite metadata while retaining the digest; a different digest for
that identity remains an integrity conflict.

## PostgreSQL destination

Set `SEARCH_CONFIGURATIONS_CONFIG_PATH` to a JSON object of revisioned destinations:

```json
{
  "documents": {
    "kind": "search-index",
    "adapter": "postgresql-search",
    "current_revision": "v1",
    "revisions": {
      "v1": {
        "namespace": "documents"
      }
    }
  }
}
```

Startup validates every retained destination revision and adapter configuration.
The synchronous DAG worker resolves the current revision for each delivery.

The built-in adapters index source identity, generic-source digest/content type, document
type, version metadata, representation descriptors, and timestamps. They do not extract or
store document text. Applications that need content indexing can provide a destination
adapter with an explicit extraction and mapping policy. PostgreSQL indexes version metadata
and namespaced representation data with GIN.

Future search backends can implement the backend-neutral `SearchAdapter` contract
using `SearchableDocument` and `IndexSubmission`, then be installed explicitly in
the search service's immutable adapter registry. A future query service or RAG
pipeline should be separate from indexing and must define authorization, namespace
isolation, ranking, citation, and prompt-injection controls before exposing indexed
content.

Multiple search destinations may coexist and are selected explicitly by each node.
The built-in adapters are `postgresql-search` and `elasticsearch`; the latter uses
the Elasticsearch-compatible create/get document APIs and therefore also supports
OpenSearch-compatible deployments.

```json
{
  "legal-search": {
    "kind": "search-index",
    "adapter": "elasticsearch",
    "current_revision": "v1",
    "revisions": {
      "v1": {
        "endpoint": "https://search.example.test:9200",
        "index": "legal-documents-v1",
        "api_key": "configured-through-a-secret",
        "timeout": 10
      }
    }
  }
}
```

Changing mapping or enrichment behavior requires a new destination revision.
Projection revision is part of index identity, so old and new projections can
coexist during a rebuild or rollout.

## Archive lifecycle

The search service has a dedicated durable consumer for archive lifecycle events.
It is separate from the `search-index` DAG worker. Only an explicit DAG node creates
a projection; `created` and `version-created` never index content implicitly.

Archive snapshots are immutable, so there is no metadata-update event or projection update
path. `version-content-removed` deletes the exact version's projections.
`deletion-requested` and `deleted` delete every projection for the archive and logical
document. These operations are idempotent and archive-scoped.

Archive remains the authoritative content and metadata store; search is a disposable
projection and must not be used as an archive.
