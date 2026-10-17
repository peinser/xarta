-- Deploy xarta_archive:00002.document-only-deletion to pg

BEGIN;

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
    ) AND NOT (
        OLD.sealed AND NEW.sealed
        AND OLD.state = 'available' AND NEW.state = 'metadata-only'
        AND NOT EXISTS (
            SELECT 1 FROM public.archive_document_representations representation
            WHERE representation.version_internal_id = OLD._id
              AND representation.state = 'available'
        )
    ) THEN
        RAISE EXCEPTION 'invalid archive version lifecycle transition' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE public.archive_document_versions DROP COLUMN history_cleanup_policy;
DROP TYPE public.archive_history_cleanup_policy;

CREATE FUNCTION public.archive_content_deletion_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'archive_document_versions' THEN
        owner_id := OLD.aggregate_id;
    ELSE
        SELECT aggregate_id INTO owner_id FROM public.archive_document_versions
        WHERE _id = OLD.version_internal_id;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.archive_documents WHERE _id = owner_id AND lifecycle = 'deleting'
    ) THEN
        RAISE EXCEPTION 'content removal requires logical document deletion' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER archive_versions_document_deletion
BEFORE UPDATE OF state ON public.archive_document_versions
FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
EXECUTE FUNCTION public.archive_content_deletion_guard();

CREATE TRIGGER archive_representations_document_deletion
BEFORE UPDATE OF state ON public.archive_document_representations
FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
EXECUTE FUNCTION public.archive_content_deletion_guard();

COMMIT;
