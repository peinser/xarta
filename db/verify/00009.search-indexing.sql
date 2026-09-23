BEGIN;

SELECT _id, id, namespace, projection_revision, source_kind, source_id, source_version, digest,
       content_type, document_type, metadata, created_at, indexed_at
FROM public.search_documents
WHERE FALSE;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'search_documents'
  AND indexname = 'search_documents_metadata_gin_idx';

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid = 'public.search_documents'::regclass
  AND contype = 'p'
  AND conkey = ARRAY[
      (SELECT attnum FROM pg_attribute
       WHERE attrelid = 'public.search_documents'::regclass AND attname = '_id')
  ]::smallint[];

SELECT 1 / attnotnull::integer
FROM pg_attribute
WHERE attrelid = 'public.search_documents'::regclass
  AND attname = 'id';

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid = 'public.search_documents'::regclass
  AND contype = 'u'
  AND conkey = ARRAY[
      (SELECT attnum FROM pg_attribute
       WHERE attrelid = 'public.search_documents'::regclass AND attname = 'id')
  ]::smallint[];

ROLLBACK;
