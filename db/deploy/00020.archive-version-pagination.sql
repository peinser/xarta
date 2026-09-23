CREATE INDEX CONCURRENTLY archive_document_versions_pagination_idx
    ON public.archive_document_versions (
        aggregate_id,
        created DESC,
        version_id DESC
    );
