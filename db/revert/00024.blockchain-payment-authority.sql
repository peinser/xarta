BEGIN;

ALTER TABLE public.node_executions DROP COLUMN admission;

CREATE TYPE public.payment_resource_type AS ENUM ('intake', 'preview');
CREATE TYPE public.payment_purchase_state AS ENUM (
    'dormant', 'settling', 'uncertain', 'completed', 'failed'
);
CREATE TYPE public.intake_admission_state AS ENUM ('dormant', 'active');

CREATE TABLE public.payment_price_quotes (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    resource text NOT NULL,
    request_fingerprint text NOT NULL,
    pricing_revision text NOT NULL,
    amount numeric NOT NULL CHECK (amount >= 0),
    currency text NOT NULL,
    quote jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);

CREATE TABLE public.payment_requirements (
    _id bigserial PRIMARY KEY,
    quote_id bigint NOT NULL UNIQUE REFERENCES public.payment_price_quotes(_id),
    scheme text NOT NULL,
    network text NOT NULL,
    amount text NOT NULL,
    asset text NOT NULL,
    pay_to text NOT NULL,
    max_timeout_seconds integer NOT NULL CHECK (max_timeout_seconds > 0),
    extra jsonb NOT NULL,
    canonical_requirements jsonb NOT NULL,
    requirements_fingerprint text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.payment_purchases (
    _id bigserial PRIMARY KEY,
    payment_id text NOT NULL UNIQUE,
    request_fingerprint text NOT NULL,
    quote_id bigint NOT NULL REFERENCES public.payment_price_quotes(_id),
    resource_type public.payment_resource_type NOT NULL,
    state public.payment_purchase_state NOT NULL,
    payment_payload jsonb,
    settlement_evidence jsonb,
    completed_response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payment_purchases_quote_idx ON public.payment_purchases (quote_id);

CREATE TABLE public.paid_intake_admissions (
    _id bigserial PRIMARY KEY,
    payment_id bigint NOT NULL UNIQUE REFERENCES public.payment_purchases(_id),
    flow_id uuid NOT NULL UNIQUE,
    prepared_flow jsonb NOT NULL,
    staged_sources jsonb NOT NULL,
    state public.intake_admission_state NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz
);

CREATE TABLE public.paid_preview_results (
    _id bigserial PRIMARY KEY,
    payment_id bigint NOT NULL UNIQUE REFERENCES public.payment_purchases(_id),
    storage_reference text NOT NULL,
    content_type text NOT NULL,
    checksum text NOT NULL,
    size bigint NOT NULL CHECK (size >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE public.payment_outbox (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    payment_id bigint NOT NULL UNIQUE REFERENCES public.payment_purchases(_id),
    subject text NOT NULL,
    payload jsonb NOT NULL,
    headers jsonb NOT NULL,
    available_at timestamptz,
    published_at timestamptz,
    lease_token uuid,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX payment_outbox_pending_idx
    ON public.payment_outbox (available_at, id)
    WHERE published_at IS NULL AND available_at IS NOT NULL;

COMMIT;
