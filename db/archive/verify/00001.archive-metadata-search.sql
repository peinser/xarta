-- Verify xarta_archive:00001.archive-metadata-search on pg
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_namespace ns ON ns.oid = idx.relnamespace
        JOIN pg_am am ON am.oid = idx.relam
        JOIN pg_attribute attr ON attr.attrelid = i.indrelid AND attr.attnum = i.indkey[0]
        JOIN pg_opclass op ON op.oid = i.indclass[0]
        WHERE ns.nspname = 'public' AND idx.relname = 'archive_document_versions_metadata_gin_idx'
          AND i.indrelid = 'public.archive_document_versions'::regclass
          AND i.indisvalid AND i.indisready AND NOT i.indisunique
          AND i.indnatts = 1 AND i.indnkeyatts = 1
          AND i.indexprs IS NULL AND i.indpred IS NULL
          AND am.amname = 'gin' AND attr.attname = 'metadata'
          AND op.opcname = 'jsonb_path_ops' AND op.opcnamespace = 'pg_catalog'::regnamespace
    ) THEN
        RAISE EXCEPTION 'Archive metadata search requires a valid, ready GIN(metadata jsonb_path_ops) index';
    END IF;
END;
$$;

ROLLBACK;
