-- Deploy xarta:00027.search-metadata-only to pg

BEGIN;

DROP INDEX public.search_documents_vector_gin_idx;

ALTER TABLE public.search_documents
    DROP COLUMN search_vector,
    DROP COLUMN extracted_text;

COMMIT;
