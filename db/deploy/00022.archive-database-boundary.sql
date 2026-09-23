-- Deploy xarta:00022.archive-database-boundary to pg

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT FROM public.archive_documents LIMIT 1) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'archive database cutover required',
            DETAIL = 'The main database still contains archive documents.',
            HINT = 'Copy archive documents, versions, idempotency rows, and sequence positions to the archive database before deploying this change.';
    END IF;
END;
$$;

DROP TABLE public.archive_idempotency;
ALTER TABLE public.archive_documents DROP CONSTRAINT archive_documents_current_version_fk;
DROP TABLE public.archive_document_versions;
DROP TABLE public.archive_documents;
DROP FUNCTION public.archive_version_guard();
DROP TYPE public.archive_history_cleanup_policy;
DROP TYPE public.archive_lifecycle;
DROP TYPE public.archive_version_state;

COMMIT;
