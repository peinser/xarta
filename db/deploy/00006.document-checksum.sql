-- Deploy xarta:00006.document-checksum to pg

BEGIN;

ALTER TABLE IF EXISTS public.documents
    ADD COLUMN checksum bytea DEFAULT NULL;

COMMIT;
