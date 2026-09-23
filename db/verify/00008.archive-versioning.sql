-- Verify xarta:00008.archive-versioning on pg

BEGIN;

SELECT to_regclass('public.archive_documents') IS NULL AS archive_moved \gset
\if :archive_moved
SELECT 1 / CASE WHEN to_regclass('public.archive_document_versions') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regclass('public.archive_idempotency') IS NULL THEN 1 ELSE 0 END;
\else

SELECT _id, archive, document_id, current_version_id, created_at, updated_at
FROM public.archive_documents WHERE FALSE;
SELECT _id, aggregate_id, version_id, predecessor_version_id, relation, created, expires,
       document_type_id, metadata, content_type, backend, backend_revision, storage_key,
       checksum, size, state
FROM public.archive_document_versions WHERE FALSE;
SELECT _id, archive, aggregate_id, idempotency_key, requested_version_id,
       request_fingerprint, response, created_at
FROM public.archive_idempotency WHERE FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'archive_document_versions_guard' AND NOT tgisinternal
    ) THEN
        RAISE EXCEPTION 'archive immutability trigger is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'archive_documents_current_version_fk' AND condeferrable
          AND conkey = ARRAY[
              (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'public.archive_documents'::regclass AND attname = '_id'),
              (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'public.archive_documents'::regclass
                 AND attname = 'current_version_id')
          ]::smallint[]
          AND confkey = ARRAY[
              (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'public.archive_document_versions'::regclass
                 AND attname = 'aggregate_id'),
              (SELECT attnum FROM pg_attribute
               WHERE attrelid = 'public.archive_document_versions'::regclass AND attname = '_id')
          ]::smallint[]
    ) THEN
        RAISE EXCEPTION 'current version ownership constraint is missing or not deferrable';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'archive_version_state' AND typtype = 'e'
    ) THEN
        RAISE EXCEPTION 'archive version state enum is missing';
    END IF;
    IF to_regclass('public.documents') IS NOT NULL THEN
        RAISE EXCEPTION 'legacy documents table was not migrated';
    END IF;
END;
$$;

SELECT 1 / CASE WHEN indexdef LIKE '%(document_type_id)%'
    AND indexdef LIKE '%WHERE (document_type_id IS NOT NULL)%'
    THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'archive_document_versions_document_type_idx';

SELECT 1 / CASE WHEN count(*) = 3 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid IN (
    'public.archive_documents'::regclass,
    'public.archive_document_versions'::regclass,
    'public.archive_idempotency'::regclass
) AND contype = 'p';

\endif

ROLLBACK;
