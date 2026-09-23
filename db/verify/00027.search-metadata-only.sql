-- Verify xarta:00027.search-metadata-only on pg

BEGIN;

SELECT 1 / CASE WHEN count(*) = 0 THEN 1 ELSE 0 END
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'search_documents'
  AND column_name IN ('extracted_text', 'search_vector');

ROLLBACK;
