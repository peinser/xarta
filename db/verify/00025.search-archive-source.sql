-- Verify xarta:00025.search-archive-source on pg

BEGIN;

SELECT source_archive, source_document_id, source_version_id, source_id, source_version,
       default_representation_id, representations
FROM public.search_documents
WHERE FALSE;

SELECT 1 / CASE WHEN count(*) = 4 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'search_documents'
  AND indexname IN (
      'search_documents_archive_identity_idx',
      'search_documents_generic_identity_idx',
      'search_documents_archive_lifecycle_idx',
      'search_documents_representations_gin_idx'
  );

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid = 'public.search_documents'::regclass
  AND conname = 'search_documents_source_shape_check';

ROLLBACK;
