-- Deploy xarta:00003.default-content-type.sql to pg

BEGIN;

ALTER TABLE IF EXISTS public.document_types
    ADD COLUMN default_content_type text NOT NULL DEFAULT 'application/pdf'::text;

COMMIT;
