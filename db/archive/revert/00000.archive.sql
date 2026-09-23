-- Revert xarta_archive:00000.archive from pg

BEGIN;

DROP TABLE public.archive_idempotency;
ALTER TABLE public.archive_documents DROP CONSTRAINT archive_documents_head_version_fk;
ALTER TABLE public.archive_document_versions
    DROP CONSTRAINT archive_document_versions_default_representation_fk;
DROP TABLE public.archive_document_representations;
DROP TABLE public.archive_document_versions;
DROP TABLE public.archive_documents;
DROP FUNCTION public.archive_document_head_guard();
DROP FUNCTION public.archive_version_must_be_sealed();
DROP FUNCTION public.archive_representation_guard();
DROP FUNCTION public.archive_version_guard();
DROP TYPE public.archive_history_cleanup_policy;
DROP TYPE public.archive_lifecycle;
DROP TYPE public.archive_version_state;

COMMIT;
