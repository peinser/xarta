-- Deploy xarta:00008.archive-versioning to pg

BEGIN;

CREATE TYPE public.archive_version_state AS ENUM ('available', 'metadata-only');

CREATE TABLE public.archive_documents (
    _id bigserial PRIMARY KEY,
    archive text NOT NULL CHECK (archive <> '' AND archive ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'),
    document_id uuid NOT NULL,
    current_version_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (archive, document_id)
);

CREATE TABLE public.archive_document_versions (
    _id bigserial PRIMARY KEY,
    aggregate_id bigint NOT NULL
        REFERENCES public.archive_documents (_id) ON DELETE CASCADE,
    version_id uuid NOT NULL UNIQUE,
    predecessor_version_id bigint,
    relation text CHECK (relation IS NULL OR relation <> ''),
    created timestamptz NOT NULL,
    expires timestamptz CHECK (expires IS NULL OR expires > created),
    document_type_id bigint REFERENCES public.document_types (_id) ON DELETE RESTRICT,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    content_type text NOT NULL CHECK (content_type <> ''),
    backend text NOT NULL CHECK (backend <> ''),
    backend_revision text NOT NULL CHECK (backend_revision <> ''),
    storage_key text,
    checksum bytea NOT NULL CHECK (octet_length(checksum) = 64),
    size bigint NOT NULL CHECK (size >= 0),
    state public.archive_version_state NOT NULL,
    CHECK (
        (state = 'available' AND storage_key IS NOT NULL AND storage_key <> '') OR
        (state = 'metadata-only' AND storage_key IS NULL)
    ),
    CHECK (predecessor_version_id IS NULL OR predecessor_version_id <> _id)
);

ALTER TABLE public.archive_document_versions
    ADD CONSTRAINT archive_document_versions_aggregate_identity_unique
    UNIQUE (aggregate_id, _id);

ALTER TABLE public.archive_document_versions
    ADD CONSTRAINT archive_document_versions_predecessor_fk
    FOREIGN KEY (predecessor_version_id)
    REFERENCES public.archive_document_versions (_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE public.archive_documents
    ADD CONSTRAINT archive_documents_current_version_fk
    FOREIGN KEY (_id, current_version_id)
    REFERENCES public.archive_document_versions (aggregate_id, _id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX archive_document_versions_storage_idx
    ON public.archive_document_versions (backend, backend_revision, storage_key)
    WHERE storage_key IS NOT NULL;
CREATE INDEX archive_document_versions_expiry_idx
    ON public.archive_document_versions (expires, aggregate_id)
    WHERE expires IS NOT NULL;
CREATE INDEX archive_document_versions_predecessor_idx
    ON public.archive_document_versions (predecessor_version_id)
    WHERE predecessor_version_id IS NOT NULL;
CREATE INDEX archive_document_versions_document_type_idx
    ON public.archive_document_versions (document_type_id)
    WHERE document_type_id IS NOT NULL;
CREATE INDEX archive_documents_current_idx
    ON public.archive_documents (current_version_id)
    WHERE current_version_id IS NOT NULL;

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

CREATE TABLE public.archive_idempotency (
    _id bigserial PRIMARY KEY,
    archive text NOT NULL CHECK (archive <> ''),
    aggregate_id bigint NOT NULL
        REFERENCES public.archive_documents (_id) ON DELETE CASCADE,
    idempotency_key text NOT NULL CHECK (idempotency_key <> ''),
    requested_version_id uuid NOT NULL,
    request_fingerprint bytea NOT NULL CHECK (octet_length(request_fingerprint) = 64),
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (archive, idempotency_key),
    UNIQUE (aggregate_id, requested_version_id)
);

CREATE INDEX archive_idempotency_created_idx
    ON public.archive_idempotency (created_at);

INSERT INTO public.archive_documents (archive, document_id, created_at, updated_at)
SELECT 'default', identifier, created, created FROM public.documents;

INSERT INTO public.archive_document_versions (
    aggregate_id, version_id, created, expires, document_type_id, metadata,
    content_type, backend, backend_revision, storage_key, checksum, size, state
)
SELECT aggregate._id, document.identifier, document.created, document.expires,
       document.document_type_id, COALESCE(document.metadata, '{}'::jsonb),
       document.content_type, 'legacy-filesystem', '00008',
       NULLIF(document.source->>'path', ''),
       COALESCE(document.checksum, decode(repeat('00', 64), 'hex')), 0,
       CASE WHEN NULLIF(document.source->>'path', '') IS NULL
            THEN 'metadata-only'::public.archive_version_state
            ELSE 'available'::public.archive_version_state END
FROM public.documents document
JOIN public.archive_documents aggregate
  ON aggregate.archive = 'default' AND aggregate.document_id = document.identifier;

UPDATE public.archive_documents aggregate
SET current_version_id = version._id
FROM public.archive_document_versions version
WHERE version.aggregate_id = aggregate._id;

DROP TABLE public.documents;

COMMIT;
