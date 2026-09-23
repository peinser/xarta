-- Revert xarta:00027.search-metadata-only from pg

BEGIN;

ALTER TABLE public.search_documents
    ADD COLUMN extracted_text text NOT NULL DEFAULT '',
    ADD COLUMN search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', extracted_text)
    ) STORED;

ALTER TABLE public.search_documents
    ALTER COLUMN extracted_text DROP DEFAULT;

CREATE INDEX search_documents_vector_gin_idx
    ON public.search_documents USING gin (search_vector);

COMMIT;
