-- Revert xarta_archive:00003.write-path-indexes from pg
-- No transaction: CONCURRENTLY rebuilds the indexes without blocking archive writes.

CREATE INDEX CONCURRENTLY IF NOT EXISTS archive_document_representations_available_version_idx
    ON public.archive_document_representations (version_internal_id, _id)
    WHERE state = 'available';
CREATE INDEX CONCURRENTLY IF NOT EXISTS archive_idempotency_created_idx
    ON public.archive_idempotency (created_at);
