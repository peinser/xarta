-- Verify xarta:00028.archive-search-without-digest on pg

BEGIN;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid = 'public.search_documents'::regclass
  AND conname = 'search_documents_digest_shape_check';

SELECT 1 / CASE WHEN attnotnull THEN 0 ELSE 1 END
FROM pg_attribute
WHERE attrelid = 'public.search_documents'::regclass
  AND attname = 'digest';

ROLLBACK;
