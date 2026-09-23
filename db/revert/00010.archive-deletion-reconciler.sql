-- Revert xarta:00010.archive-deletion-reconciler from pg

BEGIN;

DROP INDEX public.archive_document_versions_history_cleanup_idx;
DROP INDEX public.archive_document_versions_available_aggregate_idx;
DROP INDEX public.archive_documents_deleting_idx;

DROP TRIGGER archive_document_versions_guard ON public.archive_document_versions;
DROP FUNCTION public.archive_version_guard();
ALTER TABLE public.archive_document_versions
    DROP CONSTRAINT archive_document_versions_history_cleanup_check,
    DROP COLUMN history_cleanup_policy;
DROP TYPE public.archive_history_cleanup_policy;

CREATE FUNCTION public.archive_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.predecessor_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.archive_document_versions predecessor
        WHERE predecessor._id = NEW.predecessor_version_id
          AND predecessor.aggregate_id = NEW.aggregate_id
    ) THEN
        RAISE EXCEPTION 'predecessor version does not belong to archive document'
            USING ERRCODE = '23503';
    END IF;
    IF TG_OP = 'UPDATE' AND ROW(
        NEW._id, NEW.aggregate_id, NEW.version_id, NEW.predecessor_version_id,
        NEW.relation, NEW.created, NEW.expires, NEW.document_type_id, NEW.metadata,
        NEW.content_type, NEW.backend, NEW.backend_revision, NEW.checksum, NEW.size
    ) IS DISTINCT FROM ROW(
        OLD._id, OLD.aggregate_id, OLD.version_id, OLD.predecessor_version_id,
        OLD.relation, OLD.created, OLD.expires, OLD.document_type_id, OLD.metadata,
        OLD.content_type, OLD.backend, OLD.backend_revision, OLD.checksum, OLD.size
    ) THEN
        RAISE EXCEPTION 'archive version payloads are immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND NOT (
        OLD.state = 'available' AND NEW.state = 'metadata-only' AND NEW.storage_key IS NULL
    ) THEN
        RAISE EXCEPTION 'invalid archive version lifecycle transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER archive_document_versions_guard
BEFORE INSERT OR UPDATE ON public.archive_document_versions
FOR EACH ROW EXECUTE FUNCTION public.archive_version_guard();

ALTER TABLE public.archive_document_versions
    DROP CONSTRAINT archive_document_versions_predecessor_fk;
ALTER TABLE public.archive_document_versions
    ADD CONSTRAINT archive_document_versions_predecessor_fk
    FOREIGN KEY (predecessor_version_id)
    REFERENCES public.archive_document_versions (_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE public.archive_documents
    DROP CONSTRAINT archive_documents_deleted_tombstone_check,
    DROP COLUMN deleted_current_version_internal_id,
    DROP COLUMN deleted_current_version_id,
    DROP COLUMN lifecycle;
DROP TYPE public.archive_lifecycle;

COMMIT;
