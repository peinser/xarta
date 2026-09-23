-- Deploy xarta:00014.doccle-persistence to pg

BEGIN;

CREATE TYPE public.doccle_receiver_state AS ENUM (
    'pending', 'provisioning', 'provisioned', 'uncertain', 'failed'
);

CREATE TABLE public.doccle_receivers (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    destination text NOT NULL CHECK (destination <> ''),
    subject jsonb NOT NULL CHECK (
        jsonb_typeof(subject) = 'object' AND subject <> '{}'::jsonb
    ),
    external_receiver_id text NOT NULL CHECK (external_receiver_id <> ''),
    state public.doccle_receiver_state NOT NULL,
    linked boolean,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (destination, subject),
    UNIQUE (destination, external_receiver_id)
);

CREATE TABLE public.doccle_receiver_callbacks (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    destination text NOT NULL CHECK (destination <> ''),
    callback_identity text NOT NULL CHECK (callback_identity <> ''),
    receiver_id bigint NOT NULL REFERENCES public.doccle_receivers(_id),
    external_receiver_id text NOT NULL CHECK (external_receiver_id <> ''),
    linked boolean NOT NULL,
    payload jsonb NOT NULL,
    applied boolean NOT NULL DEFAULT false,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (destination, callback_identity)
);

CREATE INDEX doccle_receiver_callbacks_receiver_idx
    ON public.doccle_receiver_callbacks (receiver_id, received_at);

CREATE FUNCTION public.doccle_receiver_identity_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW._id, NEW.id, NEW.destination, NEW.subject, NEW.external_receiver_id) IS DISTINCT FROM
       ROW(OLD._id, OLD.id, OLD.destination, OLD.subject, OLD.external_receiver_id) THEN
        RAISE EXCEPTION 'Doccle receiver identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER doccle_receiver_identity_guard
BEFORE UPDATE ON public.doccle_receivers
FOR EACH ROW EXECUTE FUNCTION public.doccle_receiver_identity_guard();

COMMIT;
