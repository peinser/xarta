-- Verify xarta_archive:00003.write-path-indexes on pg

BEGIN;

SELECT 1 / CASE WHEN count(*) = 0 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public' AND indexname IN (
    'archive_document_representations_available_version_idx',
    'archive_idempotency_created_idx'
);

ROLLBACK;
