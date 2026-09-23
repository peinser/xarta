BEGIN;

CREATE TABLE public.search_documents (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    namespace text NOT NULL CHECK (
        length(namespace) BETWEEN 1 AND 128
        AND namespace ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]*$'
    ),
    projection_revision text NOT NULL CHECK (length(projection_revision) BETWEEN 1 AND 256),
    source_kind text NOT NULL CHECK (length(source_kind) BETWEEN 1 AND 64),
    source_id text NOT NULL CHECK (length(source_id) BETWEEN 1 AND 512),
    source_version text NOT NULL CHECK (length(source_version) BETWEEN 1 AND 256),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    content_type text NOT NULL CHECK (length(content_type) BETWEEN 1 AND 255),
    document_type text CHECK (
        document_type IS NULL OR length(document_type) BETWEEN 1 AND 255
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    extracted_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', extracted_text)
    ) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    indexed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace, projection_revision, source_kind, source_id, source_version)
);

CREATE INDEX search_documents_metadata_gin_idx
    ON public.search_documents USING gin (metadata jsonb_path_ops);
CREATE INDEX search_documents_vector_gin_idx
    ON public.search_documents USING gin (search_vector);

COMMIT;
