BEGIN;

CREATE TYPE public.node_execution_state AS ENUM (
    'scheduled', 'executing', 'waiting_feedback', 'resolved', 'failed'
);
CREATE TYPE public.tracked_operation_lifecycle AS ENUM (
    'open', 'uncertain', 'resolved'
);

CREATE TABLE public.node_executions (
    _id bigserial PRIMARY KEY,
    id uuid UNIQUE NOT NULL,
    flow_id uuid NOT NULL,
    node_id uuid NOT NULL,
    kind text NOT NULL,
    node jsonb NOT NULL,
    trigger_outcome_event_id bigint,
    trigger_subject jsonb,
    state public.node_execution_state NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX node_executions_root_flow_key
    ON public.node_executions (flow_id)
    WHERE trigger_outcome_event_id IS NULL;
CREATE INDEX node_executions_flow_created_idx
    ON public.node_executions (flow_id, created_at, _id);
CREATE TABLE public.execution_attempts (
    _id bigserial PRIMARY KEY,
    id uuid UNIQUE NOT NULL,
    node_execution_id bigint NOT NULL REFERENCES public.node_executions(_id),
    attempt integer NOT NULL CHECK (attempt > 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error jsonb,
    UNIQUE (node_execution_id, attempt)
);

CREATE TABLE public.tracked_operations (
    _id bigserial PRIMARY KEY,
    id uuid UNIQUE NOT NULL,
    node_execution_id bigint NOT NULL UNIQUE REFERENCES public.node_executions(_id),
    capability text NOT NULL,
    destination text NOT NULL,
    adapter text NOT NULL,
    adapter_configuration_revision text NOT NULL,
    provider_reference text,
    lifecycle public.tracked_operation_lifecycle NOT NULL,
    state jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.outcome_events (
    _id bigserial PRIMARY KEY,
    id uuid UNIQUE NOT NULL,
    flow_id uuid NOT NULL,
    node_execution_id bigint NOT NULL REFERENCES public.node_executions(_id),
    tracked_operation_id bigint REFERENCES public.tracked_operations(_id),
    outcome text NOT NULL,
    subject jsonb,
    details jsonb,
    source text NOT NULL,
    external_event_id text,
    occurred_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.node_executions
    ADD CONSTRAINT node_executions_trigger_outcome_event_fk
    FOREIGN KEY (trigger_outcome_event_id) REFERENCES public.outcome_events(_id);

CREATE INDEX node_executions_trigger_outcome_event_idx
    ON public.node_executions (trigger_outcome_event_id)
    WHERE trigger_outcome_event_id IS NOT NULL;
CREATE INDEX outcome_events_node_execution_idx
    ON public.outcome_events (node_execution_id);
CREATE INDEX outcome_events_operation_history_idx
    ON public.outcome_events (tracked_operation_id, received_at, _id)
    WHERE tracked_operation_id IS NOT NULL;
CREATE INDEX outcome_events_synchronous_idempotency_idx
    ON public.outcome_events (node_execution_id, external_event_id)
    WHERE tracked_operation_id IS NULL AND external_event_id IS NOT NULL;

CREATE TABLE public.provider_event_inbox (
    _id bigserial PRIMARY KEY,
    tracked_operation_id bigint NOT NULL REFERENCES public.tracked_operations(_id),
    adapter text NOT NULL,
    external_event_id text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tracked_operation_id, adapter, external_event_id)
);

CREATE TABLE public.branch_activations (
    _id bigserial PRIMARY KEY,
    outcome_event_id bigint NOT NULL REFERENCES public.outcome_events(_id),
    successor_node_id uuid NOT NULL,
    node_execution_id bigint NOT NULL UNIQUE REFERENCES public.node_executions(_id),
    UNIQUE (outcome_event_id, successor_node_id)
);

CREATE TABLE public.execution_outbox (
    _id bigserial PRIMARY KEY,
    id uuid UNIQUE NOT NULL,
    node_execution_id bigint NOT NULL UNIQUE REFERENCES public.node_executions(_id),
    subject text NOT NULL,
    payload jsonb NOT NULL,
    headers jsonb NOT NULL,
    available_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);

CREATE INDEX execution_outbox_pending_idx
    ON public.execution_outbox (available_at, _id)
    WHERE published_at IS NULL;

COMMIT;
