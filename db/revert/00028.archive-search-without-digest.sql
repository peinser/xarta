-- Revert xarta:00028.archive-search-without-digest from pg

BEGIN;

-- Archive projections are disposable and no longer contain the digest required
-- by the previous schema.
DELETE FROM public.search_documents WHERE source_kind = 'archive';

ALTER TABLE public.search_documents
    DROP CONSTRAINT search_documents_digest_shape_check,
    ALTER COLUMN digest SET NOT NULL,
    ADD CONSTRAINT search_documents_digest_check CHECK (
        digest ~ '^[0-9a-f]{64}$'
    );

COMMIT;
