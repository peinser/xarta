-- Verify xarta:00022.archive-database-boundary on pg

BEGIN;

SELECT 1 / CASE WHEN to_regclass('public.archive_documents') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regclass('public.archive_document_versions') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regclass('public.archive_idempotency') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regprocedure('public.archive_version_guard()') IS NULL THEN 1 ELSE 0 END;

SELECT 1 / CASE WHEN count(*) = 0 THEN 1 ELSE 0 END
FROM pg_type
WHERE typname IN (
    'archive_version_state',
    'archive_lifecycle',
    'archive_history_cleanup_policy'
) AND typnamespace = 'public'::regnamespace;

ROLLBACK;
