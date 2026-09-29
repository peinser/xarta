-- Deploy xarta:00005.sequences to pg

BEGIN;

CREATE TABLE public.sequences
(
    _id bigserial NOT NULL,
    identifier uuid NOT NULL,
    value bigint NOT NULL DEFAULT 0,
    created timestamp with time zone NOT NULL DEFAULT now(),
    PRIMARY KEY (_id),
    UNIQUE (identifier)
);

COMMIT;
