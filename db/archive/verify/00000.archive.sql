-- Verify xarta_archive:00000.archive on pg

BEGIN;

SELECT _id, archive, document_id, head_version_id, lifecycle,
       deleted_head_version_id, deleted_head_version_internal_id
FROM public.archive_documents WHERE FALSE;
SELECT _id, aggregate_id, version_id, parent_version_id, document_type,
       metadata, default_representation_id, snapshot_fingerprint, sealed, state,
       history_cleanup_policy
FROM public.archive_document_versions WHERE FALSE;
SELECT _id, version_internal_id, representation_id, name, content_type, metadata,
       backend, backend_revision, storage_key, checksum, size, state
FROM public.archive_document_representations WHERE FALSE;
SELECT _id, archive, aggregate_id, idempotency_key, request_fingerprint, response
FROM public.archive_idempotency WHERE FALSE;

SELECT 1 / CASE WHEN count(*) = 3 THEN 1 ELSE 0 END
FROM pg_type
WHERE typname IN (
    'archive_version_state',
    'archive_lifecycle',
    'archive_history_cleanup_policy'
) AND typtype = 'e';

SELECT 1 / CASE WHEN count(*) = 3 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conname IN (
    'archive_documents_head_version_fk',
    'archive_document_versions_parent_fk',
    'archive_document_versions_default_representation_fk'
) AND condeferrable;

SELECT 1 / CASE WHEN count(*) = 9 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public' AND indexname IN (
    'archive_document_representations_storage_idx',
    'archive_document_versions_expiry_idx',
    'archive_document_versions_parent_idx',
    'archive_document_versions_document_type_idx',
    'archive_documents_head_idx',
    'archive_documents_deleting_idx',
    'archive_document_representations_available_version_idx',
    'archive_document_versions_history_cleanup_idx',
    'archive_document_versions_pagination_idx'
);

SELECT 1 / CASE WHEN count(*) = 4 THEN 1 ELSE 0 END
FROM pg_trigger
WHERE tgname IN (
    'archive_document_versions_guard',
    'archive_document_representations_guard',
    'archive_document_versions_sealed',
    'archive_documents_head_guard'
) AND NOT tgisinternal;

DO $$
DECLARE
    document_internal_id bigint;
    other_document_internal_id bigint;
    stored_version_internal_id bigint;
    other_version_internal_id bigint;
BEGIN
    INSERT INTO public.archive_documents (archive, document_id)
    VALUES ('verify', '00000000-0000-0000-0000-000000000001')
    RETURNING _id INTO document_internal_id;

    INSERT INTO public.archive_document_versions (
        aggregate_id, version_id, created, metadata, default_representation_id,
        snapshot_fingerprint
    ) VALUES (
        document_internal_id, '10000000-0000-0000-0000-000000000001', now(),
        '{"business":"value"}', '20000000-0000-0000-0000-000000000001',
        decode(repeat('11', 64), 'hex')
    ) RETURNING _id INTO stored_version_internal_id;

    INSERT INTO public.archive_document_representations (
        version_internal_id, representation_id, name, content_type, metadata,
        backend, backend_revision, storage_key, checksum, size
    ) VALUES
    (
        stored_version_internal_id, '20000000-0000-0000-0000-000000000001', 'one.pdf',
        'application/pdf', '{"generator":"one"}', 'verify', 'v1', 'verify/one',
        decode(repeat('22', 64), 'hex'), 10
    ),
    (
        stored_version_internal_id, '20000000-0000-0000-0000-000000000002', 'two.pdf',
        'application/pdf', '{"generator":"two"}', 'verify', 'v1', 'verify/two',
        decode(repeat('33', 64), 'hex'), 20
    );
    UPDATE public.archive_document_versions SET sealed = true WHERE _id = stored_version_internal_id;
    UPDATE public.archive_documents SET head_version_id = stored_version_internal_id
    WHERE _id = document_internal_id;

    BEGIN
        UPDATE public.archive_document_versions SET metadata = '{}' WHERE _id = stored_version_internal_id;
        RAISE EXCEPTION 'version metadata mutation was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
    BEGIN
        UPDATE public.archive_document_versions
        SET default_representation_id = '20000000-0000-0000-0000-000000000002'
        WHERE _id = stored_version_internal_id;
        RAISE EXCEPTION 'default representation mutation was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
    BEGIN
        INSERT INTO public.archive_document_representations (
            version_internal_id, representation_id, content_type, backend,
            backend_revision, storage_key, checksum, size
        ) VALUES (
            stored_version_internal_id, '20000000-0000-0000-0000-000000000003',
            'application/xml', 'verify', 'v1', 'verify/three',
            decode(repeat('44', 64), 'hex'), 30
        );
        RAISE EXCEPTION 'sealed representation membership mutation was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
    BEGIN
        UPDATE public.archive_document_representations SET name = 'changed.pdf'
        WHERE version_internal_id = stored_version_internal_id;
        RAISE EXCEPTION 'representation mutation was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;

    INSERT INTO public.archive_documents (archive, document_id)
    VALUES ('verify', '00000000-0000-0000-0000-000000000002')
    RETURNING _id INTO other_document_internal_id;
    BEGIN
        INSERT INTO public.archive_document_versions (
            aggregate_id, version_id, parent_version_id, created,
            default_representation_id, snapshot_fingerprint
        ) VALUES (
            other_document_internal_id, '10000000-0000-0000-0000-000000000002',
            stored_version_internal_id, now(), '20000000-0000-0000-0000-000000000004',
            decode(repeat('55', 64), 'hex')
        );
        SET CONSTRAINTS archive_document_versions_parent_fk IMMEDIATE;
        RAISE EXCEPTION 'cross-document parent was accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
    BEGIN
        INSERT INTO public.archive_document_versions (
            _id, aggregate_id, version_id, parent_version_id, created,
            default_representation_id, snapshot_fingerprint
        ) VALUES (
            999999, document_internal_id, '10000000-0000-0000-0000-000000000003',
            999999, now(), '20000000-0000-0000-0000-000000000004',
            decode(repeat('66', 64), 'hex')
        );
        RAISE EXCEPTION 'self parent was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO public.archive_document_versions (
            aggregate_id, version_id, parent_version_id, created,
            default_representation_id, snapshot_fingerprint
        ) VALUES (
            document_internal_id, '10000000-0000-0000-0000-000000000004',
            stored_version_internal_id, now(), '20000000-0000-0000-0000-000000000001',
            decode(repeat('77', 64), 'hex')
        ) RETURNING _id INTO other_version_internal_id;
        INSERT INTO public.archive_document_representations (
            version_internal_id, representation_id, content_type, backend,
            backend_revision, storage_key, checksum, size
        ) VALUES (
            other_version_internal_id, '20000000-0000-0000-0000-000000000004',
            'application/xml', 'verify', 'v1', 'verify/four',
            decode(repeat('88', 64), 'hex'), 30
        );
        UPDATE public.archive_document_versions SET sealed = true
        WHERE _id = other_version_internal_id;
        SET CONSTRAINTS archive_document_versions_default_representation_fk IMMEDIATE;
        RAISE EXCEPTION 'cross-version default representation was accepted';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO public.archive_document_versions (
            aggregate_id, version_id, created, default_representation_id,
            snapshot_fingerprint
        ) VALUES (
            document_internal_id, '10000000-0000-0000-0000-000000000005', now(),
            '20000000-0000-0000-0000-000000000005', decode(repeat('99', 64), 'hex')
        );
        SET CONSTRAINTS archive_document_versions_sealed IMMEDIATE;
        RAISE EXCEPTION 'unsealed version was accepted';
    EXCEPTION WHEN check_violation THEN NULL;
    END;

    BEGIN
        UPDATE public.archive_document_representations SET storage_key = 'changed'
        WHERE representation_id = '20000000-0000-0000-0000-000000000001';
        RAISE EXCEPTION 'illegal storage lifecycle mutation was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;

    UPDATE public.archive_document_representations
    SET state = 'metadata-only', storage_key = NULL
    WHERE representation_id = '20000000-0000-0000-0000-000000000002';
    UPDATE public.archive_document_representations
    SET state = 'metadata-only', storage_key = NULL
    WHERE representation_id = '20000000-0000-0000-0000-000000000001';
    UPDATE public.archive_document_versions
    SET state = 'metadata-only', history_cleanup_policy = NULL
    WHERE _id = stored_version_internal_id;
END;
$$;

ROLLBACK;
