-- Deploy xarta:00004.document-type-can-be-null to pg

BEGIN;

ALTER TABLE IF EXISTS public.documents
    ALTER COLUMN document_type_id SET DEFAULT NULL;

ALTER TABLE IF EXISTS public.documents
    ALTER COLUMN document_type_id DROP NOT NULL;

-- XXX Add DDLs here.

COMMIT;
