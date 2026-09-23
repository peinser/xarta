-- Deploy xarta:00000.initialize to pg

BEGIN;

DROP TABLE IF EXISTS public.document_types;
CREATE TABLE public.document_types
(
    _id bigserial NOT NULL,
    identifier text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_business_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    business_data_schema jsonb,
    default_template_engine_options jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_template_engine text DEFAULT NULL,
    created timestamp with time zone NOT NULL DEFAULT now(),
    default_retention interval DEFAULT NULL,
    PRIMARY KEY (_id),
    UNIQUE (identifier),
    CHECK (LENGTH(identifier) <= 512)
);

COMMIT;
