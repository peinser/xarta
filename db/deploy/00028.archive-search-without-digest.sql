-- Deploy xarta:00028.archive-search-without-digest to pg

BEGIN;

ALTER TABLE public.search_documents
    DROP CONSTRAINT search_documents_digest_check,
    ALTER COLUMN digest DROP NOT NULL;

UPDATE public.search_documents
SET digest = NULL
WHERE source_kind = 'archive';

ALTER TABLE public.search_documents
    ADD CONSTRAINT search_documents_digest_shape_check CHECK (
        (source_kind = 'archive' AND digest IS NULL)
        OR
        (source_kind <> 'archive' AND digest ~ '^[0-9a-f]{64}$')
    );

COMMIT;
