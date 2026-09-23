BEGIN;

ALTER TABLE public.node_executions
    ADD COLUMN trigger_outcome text,
    ADD COLUMN trigger_outcome_event_public_id uuid,
    ADD COLUMN trigger_originating_node_execution_id bigint
        REFERENCES public.node_executions(_id),
    ADD COLUMN trigger_originating_node_execution_public_id uuid;

UPDATE public.node_executions AS execution
SET trigger_outcome = event.outcome,
    trigger_outcome_event_public_id = event.id,
    trigger_originating_node_execution_id = event.node_execution_id,
    trigger_originating_node_execution_public_id = origin.id
FROM public.outcome_events AS event
JOIN public.node_executions AS origin ON origin._id = event.node_execution_id
WHERE execution.trigger_outcome_event_id = event._id;

ALTER TABLE public.node_executions
    ADD CONSTRAINT node_executions_trigger_identity_check CHECK (
        (
            trigger_outcome_event_public_id IS NULL
            AND trigger_outcome IS NULL
            AND trigger_originating_node_execution_public_id IS NULL
            AND trigger_subject IS NULL
        )
        OR
        (
            trigger_outcome_event_public_id IS NOT NULL
            AND trigger_outcome IS NOT NULL
            AND trigger_originating_node_execution_public_id IS NOT NULL
        )
    );

DROP INDEX public.node_executions_root_flow_key;
CREATE UNIQUE INDEX node_executions_root_flow_key
    ON public.node_executions (flow_id)
    WHERE trigger_outcome_event_public_id IS NULL;

CREATE INDEX node_executions_trigger_originating_idx
    ON public.node_executions (trigger_originating_node_execution_id)
    WHERE trigger_originating_node_execution_id IS NOT NULL;
CREATE INDEX node_executions_trigger_outcome_event_public_idx
    ON public.node_executions (trigger_outcome_event_public_id)
    WHERE trigger_outcome_event_public_id IS NOT NULL;
CREATE INDEX node_executions_trigger_originating_public_idx
    ON public.node_executions (trigger_originating_node_execution_public_id)
    WHERE trigger_originating_node_execution_public_id IS NOT NULL;

COMMIT;
