-- Verify xarta_archive:00002.document-only-deletion on pg

BEGIN;

SELECT 1 / CASE WHEN NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'archive_document_versions'
      AND column_name = 'history_cleanup_policy'
) THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN to_regtype('public.archive_history_cleanup_policy') IS NULL THEN 1 ELSE 0 END;
SELECT 1 / CASE WHEN count(*) = 2 THEN 1 ELSE 0 END FROM pg_trigger
WHERE tgname IN ('archive_versions_document_deletion', 'archive_representations_document_deletion')
  AND NOT tgisinternal AND tgenabled = 'O';

DO $$
DECLARE
    document_internal_id bigint;
    version_internal_id bigint;
BEGIN
    INSERT INTO public.archive_documents (archive, document_id)
    VALUES ('verify-document-deletion', '00000000-0000-0000-0000-000000000001')
    RETURNING _id INTO document_internal_id;
    INSERT INTO public.archive_document_versions (
        aggregate_id, version_id, created, default_representation_id, snapshot_fingerprint
    ) VALUES (
        document_internal_id, '10000000-0000-0000-0000-000000000001', now(),
        '20000000-0000-0000-0000-000000000001', decode(repeat('11', 64), 'hex')
    ) RETURNING _id INTO version_internal_id;
    INSERT INTO public.archive_document_representations (
        version_internal_id, representation_id, content_type, backend, backend_revision,
        storage_key, checksum, size
    ) VALUES (
        version_internal_id, '20000000-0000-0000-0000-000000000001', 'text/plain',
        'verify', 'v1', 'verify-document-deletion/invoice', decode(repeat('22', 64), 'hex'), 7
    );
    UPDATE public.archive_document_versions SET sealed = true WHERE _id = version_internal_id;
    UPDATE public.archive_documents SET head_version_id = version_internal_id
    WHERE _id = document_internal_id;

    BEGIN
        UPDATE public.archive_document_representations SET state = 'metadata-only', storage_key = NULL
        WHERE representation_id = '20000000-0000-0000-0000-000000000001';
        RAISE EXCEPTION 'active document content removal was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;

    UPDATE public.archive_documents SET lifecycle = 'deleting' WHERE _id = document_internal_id;
    UPDATE public.archive_document_representations SET state = 'metadata-only', storage_key = NULL
    WHERE representation_id = '20000000-0000-0000-0000-000000000001';
    UPDATE public.archive_document_versions SET state = 'metadata-only' WHERE _id = version_internal_id;
    SET CONSTRAINTS ALL IMMEDIATE;
END;
$$;

ROLLBACK;
