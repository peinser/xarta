-- Revert xarta_archive:00002.document-only-deletion from pg

BEGIN;

DROP TRIGGER archive_versions_document_deletion ON public.archive_document_versions;
DROP TRIGGER archive_representations_document_deletion ON public.archive_document_representations;
DROP FUNCTION public.archive_content_deletion_guard();

CREATE TYPE public.archive_history_cleanup_policy AS ENUM ('retain-metadata', 'latest-only');
ALTER TABLE public.archive_document_versions
    ADD COLUMN history_cleanup_policy public.archive_history_cleanup_policy,
    ADD CONSTRAINT archive_document_versions_history_cleanup_check
        CHECK (history_cleanup_policy IS NULL OR state = 'available');
CREATE INDEX archive_document_versions_history_cleanup_idx
    ON public.archive_document_versions (aggregate_id, _id)
    WHERE history_cleanup_policy IS NOT NULL;

-- Removed cleanup intents are deliberately not recreated on rollback.
CREATE OR REPLACE FUNCTION public.archive_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND ROW(
        NEW._id, NEW.aggregate_id, NEW.version_id, NEW.parent_version_id,
        NEW.created, NEW.expires, NEW.document_type, NEW.metadata,
        NEW.default_representation_id, NEW.snapshot_fingerprint
    ) IS DISTINCT FROM ROW(
        OLD._id, OLD.aggregate_id, OLD.version_id, OLD.parent_version_id,
        OLD.created, OLD.expires, OLD.document_type, OLD.metadata,
        OLD.default_representation_id, OLD.snapshot_fingerprint
    ) THEN
        RAISE EXCEPTION 'archive version snapshots are immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND NOT (
        NOT OLD.sealed AND NEW.sealed AND OLD.state = NEW.state
        AND OLD.history_cleanup_policy IS NOT DISTINCT FROM NEW.history_cleanup_policy
    ) AND NOT (
        OLD.sealed AND NEW.sealed AND OLD.state = 'available' AND NEW.state = 'available'
        AND OLD.history_cleanup_policy IS NULL AND NEW.history_cleanup_policy IS NOT NULL
    ) AND NOT (
        OLD.sealed AND NEW.sealed AND OLD.state = 'available' AND NEW.state = 'metadata-only'
        AND NEW.history_cleanup_policy IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM public.archive_document_representations representation
            WHERE representation.version_internal_id = OLD._id AND representation.state = 'available'
        )
    ) THEN
        RAISE EXCEPTION 'invalid archive version lifecycle transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

COMMIT;
