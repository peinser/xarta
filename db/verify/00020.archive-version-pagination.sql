BEGIN;

SELECT to_regclass('public.archive_document_versions') IS NULL AS archive_moved \gset
\if :archive_moved
SELECT 1;
\else

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'archive_document_versions_pagination_idx'
  AND indexdef LIKE '%(aggregate_id, created DESC, version_id DESC)%';

SELECT 1 / CASE WHEN count(*) = 0 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'archive_document_versions_history_idx';

\endif

ROLLBACK;
