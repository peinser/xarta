-- Deploy xarta:00001.documents to pg

BEGIN;

CREATE TABLE public.documents
(
    _id bigserial NOT NULL,
    document_type_id bigint NOT NULL,
    identifier uuid NOT NULL,
    created timestamp with time zone NOT NULL DEFAULT now(),
    expires timestamp with time zone DEFAULT NULL,
    metadata jsonb DEFAULT NULL,
    content_type text NOT NULL DEFAULT 'application/pdf',
    source jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (_id),
    UNIQUE (identifier),
    FOREIGN KEY (document_type_id)
        REFERENCES public.document_types (_id) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE CASCADE
        NOT VALID
);

COMMIT;
