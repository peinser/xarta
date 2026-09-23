-- Deploy xarta:00010.archive-deletion-reconciler to pg

BEGIN;

CREATE TYPE public.archive_lifecycle AS ENUM ('active', 'deleting', 'deleted');
CREATE TYPE public.archive_history_cleanup_policy AS ENUM (
    'retain-metadata',
    'latest-only'
);

ALTER TABLE public.archive_documents
    ADD COLUMN lifecycle public.archive_lifecycle NOT NULL DEFAULT 'active',
    ADD COLUMN deleted_current_version_id uuid,
    ADD COLUMN deleted_current_version_internal_id bigint,
    ADD CONSTRAINT archive_documents_deleted_tombstone_check CHECK (
        (lifecycle = 'deleted' AND current_version_id IS NULL
            AND deleted_current_version_id IS NOT NULL
            AND deleted_current_version_internal_id IS NOT NULL) OR
        (lifecycle <> 'deleted' AND deleted_current_version_id IS NULL
            AND deleted_current_version_internal_id IS NULL)
    );

ALTER TABLE public.archive_document_versions
    ADD COLUMN history_cleanup_policy public.archive_history_cleanup_policy,
    ADD CONSTRAINT archive_document_versions_history_cleanup_check
        CHECK (history_cleanup_policy IS NULL OR state = 'available');

ALTER TABLE public.archive_document_versions
    DROP CONSTRAINT archive_document_versions_predecessor_fk;
ALTER TABLE public.archive_document_versions
    ADD CONSTRAINT archive_document_versions_predecessor_fk
    FOREIGN KEY (predecessor_version_id)
    REFERENCES public.archive_document_versions (_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX archive_documents_deleting_idx
    ON public.archive_documents (_id, current_version_id)
    WHERE lifecycle = 'deleting';
CREATE INDEX archive_document_versions_available_aggregate_idx
    ON public.archive_document_versions (aggregate_id, _id)
    WHERE state = 'available';
CREATE INDEX archive_document_versions_history_cleanup_idx
    ON public.archive_document_versions (aggregate_id, _id)
    WHERE history_cleanup_policy IS NOT NULL;

CREATE OR REPLACE FUNCTION public.archive_version_guard() RETURNS trigger
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
        OLD.state = 'available' AND NEW.state = 'available'
        AND OLD.storage_key = NEW.storage_key
        AND OLD.history_cleanup_policy IS NULL
        AND NEW.history_cleanup_policy IS NOT NULL
    ) AND NOT (
        OLD.state = 'available' AND NEW.state = 'metadata-only'
        AND NEW.storage_key IS NULL AND NEW.history_cleanup_policy IS NULL
    ) THEN
        RAISE EXCEPTION 'invalid archive version lifecycle transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
