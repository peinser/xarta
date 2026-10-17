-- Revert xarta_archive:00001.archive-metadata-search from pg
-- No transaction: do not block archive writes while removing the index.
DROP INDEX CONCURRENTLY IF EXISTS public.archive_document_versions_metadata_gin_idx;
