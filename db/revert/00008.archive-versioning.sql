-- Revert xarta:00008.archive-versioning from pg

BEGIN;

CREATE TABLE public.documents (
    _id bigserial PRIMARY KEY,
    document_type_id bigint REFERENCES public.document_types (_id) ON DELETE CASCADE,
    identifier uuid NOT NULL UNIQUE,
    created timestamptz NOT NULL DEFAULT now(),
    expires timestamptz,
    metadata jsonb,
    content_type text NOT NULL DEFAULT 'application/pdf',
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    checksum bytea
);

INSERT INTO public.documents (
    document_type_id, identifier, created, expires, metadata, content_type, source, checksum
)
SELECT version.document_type_id, aggregate.document_id, version.created, version.expires,
       version.metadata, version.content_type,
       CASE WHEN version.storage_key IS NULL THEN '{}'::jsonb
            ELSE jsonb_build_object('path', version.storage_key) END,
       version.checksum
FROM public.archive_documents aggregate
JOIN public.archive_document_versions version
  ON version._id = aggregate.current_version_id
WHERE aggregate.archive = 'default';

DROP TABLE public.archive_idempotency;
ALTER TABLE public.archive_documents DROP CONSTRAINT archive_documents_current_version_fk;
DROP TABLE public.archive_document_versions;
DROP TABLE public.archive_documents;
DROP FUNCTION public.archive_version_guard();
DROP TYPE public.archive_version_state;

COMMIT;
