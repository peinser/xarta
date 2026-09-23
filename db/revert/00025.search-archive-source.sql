-- Revert xarta:00025.search-archive-source from pg

BEGIN;

TRUNCATE public.search_documents;

DROP INDEX public.search_documents_archive_lifecycle_idx;
DROP INDEX public.search_documents_generic_identity_idx;
DROP INDEX public.search_documents_archive_identity_idx;
DROP INDEX public.search_documents_representations_gin_idx;
ALTER TABLE public.search_documents
    DROP CONSTRAINT search_documents_source_shape_check,
    DROP COLUMN source_archive,
    DROP COLUMN source_document_id,
    DROP COLUMN source_version_id,
    DROP COLUMN source_id,
    DROP COLUMN source_version,
    DROP COLUMN default_representation_id,
    DROP COLUMN representations,
    ALTER COLUMN content_type SET NOT NULL,
    ADD COLUMN source_id text NOT NULL CHECK (length(source_id) BETWEEN 1 AND 512),
    ADD COLUMN source_version text NOT NULL CHECK (length(source_version) BETWEEN 1 AND 256),
    ADD UNIQUE (namespace, projection_revision, source_kind, source_id, source_version);

COMMIT;
