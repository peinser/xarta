-- Deploy xarta_archive:00000.archive to pg

BEGIN;

CREATE TYPE public.archive_version_state AS ENUM ('available', 'metadata-only');
CREATE TYPE public.archive_lifecycle AS ENUM ('active', 'deleting', 'deleted');
CREATE TYPE public.archive_history_cleanup_policy AS ENUM ('retain-metadata', 'latest-only');

CREATE TABLE public.archive_documents (
    _id bigserial PRIMARY KEY,
    archive text NOT NULL CHECK (archive <> '' AND archive ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'),
    document_id uuid NOT NULL,
    head_version_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    lifecycle public.archive_lifecycle NOT NULL DEFAULT 'active',
    deleted_head_version_id uuid,
    deleted_head_version_internal_id bigint,
    UNIQUE (archive, document_id),
    CONSTRAINT archive_documents_deleted_tombstone_check CHECK (
        (lifecycle = 'deleted' AND head_version_id IS NULL
            AND deleted_head_version_id IS NOT NULL
            AND deleted_head_version_internal_id IS NOT NULL) OR
        (lifecycle <> 'deleted' AND deleted_head_version_id IS NULL
            AND deleted_head_version_internal_id IS NULL)
    )
);

CREATE TABLE public.archive_document_versions (
    _id bigserial PRIMARY KEY,
    aggregate_id bigint NOT NULL REFERENCES public.archive_documents (_id) ON DELETE CASCADE,
    version_id uuid NOT NULL,
    parent_version_id bigint,
    created timestamptz NOT NULL,
    expires timestamptz CHECK (expires IS NULL OR expires > created),
    document_type text CHECK (document_type IS NULL OR document_type <> ''),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    default_representation_id uuid NOT NULL,
    snapshot_fingerprint bytea NOT NULL CHECK (octet_length(snapshot_fingerprint) = 64),
    sealed boolean NOT NULL DEFAULT false,
    state public.archive_version_state NOT NULL DEFAULT 'available',
    history_cleanup_policy public.archive_history_cleanup_policy,
    CHECK (parent_version_id IS NULL OR parent_version_id <> _id),
    CONSTRAINT archive_document_versions_history_cleanup_check
        CHECK (history_cleanup_policy IS NULL OR state = 'available'),
    CONSTRAINT archive_document_versions_aggregate_identity_unique UNIQUE (aggregate_id, _id),
    CONSTRAINT archive_document_versions_aggregate_version_unique UNIQUE (aggregate_id, version_id),
    CONSTRAINT archive_document_versions_parent_fk
        FOREIGN KEY (aggregate_id, parent_version_id)
        REFERENCES public.archive_document_versions (aggregate_id, _id)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE public.archive_document_representations (
    _id bigserial PRIMARY KEY,
    version_internal_id bigint NOT NULL
        REFERENCES public.archive_document_versions (_id) ON DELETE CASCADE,
    representation_id uuid NOT NULL UNIQUE,
    name text CHECK (name IS NULL OR (btrim(name) <> '' AND name !~ '[[:cntrl:]]')),
    content_type text NOT NULL CHECK (
        content_type ~ '^[A-Za-z0-9!#$%&*+.^_`|~-]+/[A-Za-z0-9!#$%&*+.^_`|~-]+$'
    ),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    backend text NOT NULL CHECK (backend <> ''),
    backend_revision text NOT NULL CHECK (backend_revision <> ''),
    storage_key text,
    checksum bytea NOT NULL CHECK (octet_length(checksum) = 64),
    size bigint NOT NULL CHECK (size >= 0),
    state public.archive_version_state NOT NULL DEFAULT 'available',
    CHECK (
        (state = 'available' AND storage_key IS NOT NULL AND storage_key <> '') OR
        (state = 'metadata-only' AND storage_key IS NULL)
    ),
    CONSTRAINT archive_document_representations_version_identity_unique
        UNIQUE (version_internal_id, representation_id)
);

ALTER TABLE public.archive_document_versions
    ADD CONSTRAINT archive_document_versions_default_representation_fk
    FOREIGN KEY (_id, default_representation_id)
    REFERENCES public.archive_document_representations (version_internal_id, representation_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE public.archive_documents
    ADD CONSTRAINT archive_documents_head_version_fk
    FOREIGN KEY (_id, head_version_id)
    REFERENCES public.archive_document_versions (aggregate_id, _id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX archive_document_representations_storage_idx
    ON public.archive_document_representations (backend, backend_revision, storage_key)
    WHERE storage_key IS NOT NULL;
CREATE INDEX archive_document_versions_expiry_idx
    ON public.archive_document_versions (expires, aggregate_id) WHERE expires IS NOT NULL;
CREATE INDEX archive_document_versions_parent_idx
    ON public.archive_document_versions (parent_version_id) WHERE parent_version_id IS NOT NULL;
CREATE INDEX archive_document_versions_document_type_idx
    ON public.archive_document_versions (document_type) WHERE document_type IS NOT NULL;
CREATE INDEX archive_documents_head_idx
    ON public.archive_documents (head_version_id) WHERE head_version_id IS NOT NULL;
CREATE INDEX archive_documents_deleting_idx
    ON public.archive_documents (_id, head_version_id) WHERE lifecycle = 'deleting';
CREATE INDEX archive_document_representations_available_version_idx
    ON public.archive_document_representations (version_internal_id, _id)
    WHERE state = 'available';
CREATE INDEX archive_document_versions_history_cleanup_idx
    ON public.archive_document_versions (aggregate_id, _id)
    WHERE history_cleanup_policy IS NOT NULL;
CREATE INDEX archive_document_versions_pagination_idx
    ON public.archive_document_versions (aggregate_id, created DESC, version_id DESC);

CREATE FUNCTION public.archive_version_guard() RETURNS trigger
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
        NOT OLD.sealed AND NEW.sealed
        AND OLD.state = NEW.state
        AND OLD.history_cleanup_policy IS NOT DISTINCT FROM NEW.history_cleanup_policy
    ) AND NOT (
        OLD.sealed AND NEW.sealed
        AND OLD.state = 'available' AND NEW.state = 'available'
        AND OLD.history_cleanup_policy IS NULL
        AND NEW.history_cleanup_policy IS NOT NULL
    ) AND NOT (
        OLD.sealed AND NEW.sealed
        AND OLD.state = 'available' AND NEW.state = 'metadata-only'
        AND NEW.history_cleanup_policy IS NULL
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

CREATE FUNCTION public.archive_representation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    version_sealed boolean;
    document_lifecycle public.archive_lifecycle;
BEGIN
    SELECT version.sealed, aggregate.lifecycle
    INTO version_sealed, document_lifecycle
    FROM public.archive_document_versions version
    JOIN public.archive_documents aggregate ON aggregate._id = version.aggregate_id
    WHERE version._id = COALESCE(NEW.version_internal_id, OLD.version_internal_id);

    IF TG_OP = 'INSERT' AND version_sealed THEN
        RAISE EXCEPTION 'archive version representation membership is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' AND version_sealed AND document_lifecycle <> 'deleting' THEN
        RAISE EXCEPTION 'archive version representation membership is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND ROW(
        NEW._id, NEW.version_internal_id, NEW.representation_id, NEW.name,
        NEW.content_type, NEW.metadata, NEW.backend, NEW.backend_revision,
        NEW.checksum, NEW.size
    ) IS DISTINCT FROM ROW(
        OLD._id, OLD.version_internal_id, OLD.representation_id, OLD.name,
        OLD.content_type, OLD.metadata, OLD.backend, OLD.backend_revision,
        OLD.checksum, OLD.size
    ) THEN
        RAISE EXCEPTION 'archive representations are immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND NOT (
        OLD.state = 'available' AND NEW.state = 'metadata-only'
        AND OLD.storage_key IS NOT NULL AND NEW.storage_key IS NULL
    ) THEN
        RAISE EXCEPTION 'invalid archive representation lifecycle transition'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.archive_version_must_be_sealed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.archive_document_versions
        WHERE _id = NEW._id AND NOT sealed
    ) THEN
        RAISE EXCEPTION 'archive version must be sealed before commit' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.archive_document_head_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.head_version_id IS DISTINCT FROM OLD.head_version_id
       AND NEW.head_version_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM public.archive_document_versions version
           WHERE version._id = NEW.head_version_id
             AND version.aggregate_id = NEW._id
             AND version.sealed
             AND version.parent_version_id IS NOT DISTINCT FROM OLD.head_version_id
       ) THEN
        RAISE EXCEPTION 'archive HEAD must advance to a sealed child version'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER archive_document_versions_guard
BEFORE UPDATE ON public.archive_document_versions
FOR EACH ROW EXECUTE FUNCTION public.archive_version_guard();

CREATE TRIGGER archive_document_representations_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.archive_document_representations
FOR EACH ROW EXECUTE FUNCTION public.archive_representation_guard();

CREATE CONSTRAINT TRIGGER archive_document_versions_sealed
AFTER INSERT OR UPDATE ON public.archive_document_versions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.archive_version_must_be_sealed();

CREATE TRIGGER archive_documents_head_guard
BEFORE UPDATE OF head_version_id ON public.archive_documents
FOR EACH ROW EXECUTE FUNCTION public.archive_document_head_guard();

CREATE TABLE public.archive_idempotency (
    _id bigserial PRIMARY KEY,
    archive text NOT NULL CHECK (archive <> ''),
    aggregate_id bigint NOT NULL REFERENCES public.archive_documents (_id) ON DELETE CASCADE,
    idempotency_key text NOT NULL CHECK (idempotency_key <> ''),
    requested_version_id uuid NOT NULL,
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 64),
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (archive, idempotency_key),
    UNIQUE (aggregate_id, requested_version_id)
);

CREATE INDEX archive_idempotency_created_idx ON public.archive_idempotency (created_at);

COMMIT;
