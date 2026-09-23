-- Verify xarta:00010.archive-deletion-reconciler on pg

BEGIN;

SELECT to_regclass('public.archive_documents') IS NULL AS archive_moved \gset
\if :archive_moved
SELECT 1 / CASE WHEN to_regclass('public.archive_document_versions') IS NULL THEN 1 ELSE 0 END;
\else

SELECT lifecycle, deleted_current_version_id, deleted_current_version_internal_id
FROM public.archive_documents WHERE FALSE;
SELECT history_cleanup_policy FROM public.archive_document_versions WHERE FALSE;

SELECT 1 / CASE WHEN count(*) = 2 THEN 1 ELSE 0 END
FROM pg_type
WHERE typname IN ('archive_lifecycle', 'archive_history_cleanup_policy')
  AND typtype = 'e';

SELECT 1 / CASE WHEN array_agg(enumlabel::text ORDER BY enumsortorder) =
    ARRAY['retain-metadata', 'latest-only'] THEN 1 ELSE 0 END
FROM pg_enum
WHERE enumtypid = 'public.archive_history_cleanup_policy'::regtype;

SELECT 1 / CASE WHEN array_agg(enumlabel::text ORDER BY enumsortorder) =
    ARRAY['active', 'deleting', 'deleted'] THEN 1 ELSE 0 END
FROM pg_enum
WHERE enumtypid = 'public.archive_lifecycle'::regtype;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conname = 'archive_document_versions_history_cleanup_check'
  AND conrelid = 'public.archive_document_versions'::regclass;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conname = 'archive_documents_deleted_tombstone_check'
  AND conrelid = 'public.archive_documents'::regclass;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conname = 'archive_document_versions_predecessor_fk'
  AND confdeltype = 'c' AND condeferrable;

SELECT 1 / CASE WHEN count(*) = 3 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
      'archive_documents_deleting_idx',
      'archive_document_versions_available_aggregate_idx',
      'archive_document_versions_history_cleanup_idx'
  );

SELECT 1 / CASE WHEN indexdef LIKE '%(_id, current_version_id)%'
    AND indexdef LIKE '%WHERE (lifecycle = ''deleting''::archive_lifecycle)%'
    THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public' AND indexname = 'archive_documents_deleting_idx';

SELECT 1 / CASE WHEN indexdef LIKE '%(aggregate_id, _id)%'
    AND indexdef LIKE '%WHERE (state = ''available''::archive_version_state)%'
    THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'archive_document_versions_available_aggregate_idx';

SELECT 1 / CASE WHEN indexdef LIKE '%(aggregate_id, _id)%'
    AND indexdef LIKE '%WHERE (history_cleanup_policy IS NOT NULL)%'
    THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'archive_document_versions_history_cleanup_idx';

SELECT 1 / CASE WHEN to_regclass('public.archive_deletion_jobs') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regclass('public.archive_event_outbox') IS NULL THEN 1 ELSE 0 END;

\endif

ROLLBACK;
