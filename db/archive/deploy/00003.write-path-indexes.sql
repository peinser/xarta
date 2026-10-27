-- Deploy xarta_archive:00003.write-path-indexes to pg
-- No transaction: CONCURRENTLY drops the indexes without blocking archive writes.

-- Lookups by version are served by archive_document_representations_version_identity_unique.
DROP INDEX CONCURRENTLY IF EXISTS public.archive_document_representations_available_version_idx;
-- No query reads archive_idempotency by created_at.
DROP INDEX CONCURRENTLY IF EXISTS public.archive_idempotency_created_idx;
