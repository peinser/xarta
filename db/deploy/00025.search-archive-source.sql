-- Deploy xarta:00025.search-archive-source to pg

BEGIN;

-- Search is a disposable projection. Existing rows lack the archive namespace
-- required to identify their source and must be explicitly indexed again.
TRUNCATE public.search_documents;

ALTER TABLE public.search_documents
    DROP COLUMN source_id CASCADE,
    DROP COLUMN source_version CASCADE,
    ADD COLUMN source_archive text,
    ADD COLUMN source_document_id uuid,
    ADD COLUMN source_version_id uuid,
    ADD COLUMN source_id text,
    ADD COLUMN source_version text,
    ADD COLUMN default_representation_id uuid,
    ADD COLUMN representations jsonb,
    ALTER COLUMN content_type DROP NOT NULL,
    ADD CONSTRAINT search_documents_source_shape_check CHECK (
        (source_kind = 'archive'
            AND source_archive IS NOT NULL AND source_archive <> ''
            AND source_document_id IS NOT NULL AND source_version_id IS NOT NULL
            AND source_id IS NULL AND source_version IS NULL
            AND content_type IS NULL
            AND default_representation_id IS NOT NULL
            AND jsonb_typeof(representations) = 'array'
            AND jsonb_array_length(representations) > 0)
        OR
        (source_kind <> 'archive'
            AND source_archive IS NULL
            AND source_document_id IS NULL AND source_version_id IS NULL
            AND source_id IS NOT NULL AND source_id <> ''
            AND source_version IS NOT NULL AND source_version <> ''
            AND content_type IS NOT NULL
            AND default_representation_id IS NULL
            AND representations = '[]'::jsonb)
    );

CREATE UNIQUE INDEX search_documents_archive_identity_idx
    ON public.search_documents (
        namespace, projection_revision, source_archive,
        source_document_id, source_version_id
    ) WHERE source_kind = 'archive';
CREATE UNIQUE INDEX search_documents_generic_identity_idx
    ON public.search_documents (
        namespace, projection_revision, source_kind, source_id, source_version
    ) WHERE source_kind <> 'archive';
CREATE INDEX search_documents_archive_lifecycle_idx
    ON public.search_documents (
        namespace, source_archive, source_document_id, source_version_id
    ) WHERE source_kind = 'archive';
CREATE INDEX search_documents_representations_gin_idx
    ON public.search_documents USING gin (representations jsonb_path_ops)
    WHERE source_kind = 'archive';

COMMIT;
