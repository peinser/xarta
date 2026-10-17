-- Deploy xarta_archive:00001.archive-metadata-search to pg
-- No transaction: CONCURRENTLY allows archive writes during the index build.
-- An interrupted build can leave an invalid index. Repair only our expected index.
SELECT format('DROP INDEX CONCURRENTLY %I.%I', ns.nspname, idx.relname)
FROM pg_index i
JOIN pg_class idx ON idx.oid = i.indexrelid
JOIN pg_namespace ns ON ns.oid = idx.relnamespace
JOIN pg_am am ON am.oid = idx.relam
JOIN pg_attribute attr ON attr.attrelid = i.indrelid AND attr.attnum = i.indkey[0]
JOIN pg_opclass op ON op.oid = i.indclass[0]
WHERE ns.nspname = 'public' AND idx.relname = 'archive_document_versions_metadata_gin_idx'
  AND i.indrelid = 'public.archive_document_versions'::regclass
  AND NOT i.indisvalid AND am.amname = 'gin' AND attr.attname = 'metadata'
  AND op.opcname = 'jsonb_path_ops' AND op.opcnamespace = 'pg_catalog'::regnamespace
  AND i.indnatts = 1 AND i.indexprs IS NULL AND i.indpred IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM pg_stat_progress_create_index progress
      WHERE progress.index_relid = i.indexrelid
  )
\gexec

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_stat_progress_create_index
        WHERE index_relid = to_regclass('public.archive_document_versions_metadata_gin_idx')
    ) THEN
        RAISE EXCEPTION 'Archive metadata index build is still running; retry after it completes or is cancelled';
    END IF;
END;
$$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS archive_document_versions_metadata_gin_idx
    ON public.archive_document_versions USING gin (metadata jsonb_path_ops);

-- IF NOT EXISTS must not silently accept a valid but differently defined index.
\ir ../verify/00001.archive-metadata-search.sql
