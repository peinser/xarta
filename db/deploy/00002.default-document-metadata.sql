-- Deploy xarta:00002.default-document-metadata to pg

BEGIN;

ALTER TABLE IF EXISTS public.document_types
    ADD COLUMN default_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
