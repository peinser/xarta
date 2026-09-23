BEGIN;

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

ALTER TABLE public.node_executions
    DROP COLUMN correlation_id;

COMMIT;
