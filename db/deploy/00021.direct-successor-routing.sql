BEGIN;

ALTER TABLE public.node_executions
    ADD COLUMN IF NOT EXISTS trigger_outcome_event_public_id uuid,
    ADD COLUMN IF NOT EXISTS trigger_originating_node_execution_public_id uuid,
    ADD COLUMN correlation_id uuid;

UPDATE public.node_executions AS execution
SET trigger_outcome_event_public_id = event.id,
    trigger_originating_node_execution_public_id = origin.id
FROM public.outcome_events AS event
JOIN public.node_executions AS origin ON origin._id = event.node_execution_id
WHERE execution.trigger_outcome_event_id = event._id
  AND execution.trigger_outcome_event_public_id IS NULL;

ALTER TABLE public.node_executions
    DROP CONSTRAINT IF EXISTS node_executions_trigger_identity_check,
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

DROP INDEX IF EXISTS public.node_executions_root_flow_key;
CREATE UNIQUE INDEX node_executions_root_flow_key
    ON public.node_executions (flow_id)
    WHERE trigger_outcome_event_public_id IS NULL;

CREATE INDEX IF NOT EXISTS node_executions_trigger_outcome_event_public_idx
    ON public.node_executions (trigger_outcome_event_public_id)
    WHERE trigger_outcome_event_public_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS node_executions_trigger_originating_public_idx
    ON public.node_executions (trigger_originating_node_execution_public_id)
    WHERE trigger_originating_node_execution_public_id IS NOT NULL;

UPDATE public.node_executions AS execution
SET correlation_id = COALESCE(
    (
        SELECT (outbox.headers->>'correlation-id')::uuid
        FROM public.execution_outbox AS outbox
        WHERE outbox.node_execution_id = execution._id
    ),
    execution.flow_id
);

ALTER TABLE public.node_executions
    ALTER COLUMN correlation_id SET NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT FROM public.execution_outbox WHERE published_at IS NULL) THEN
        RAISE EXCEPTION
            'Cannot remove execution_outbox while unpublished handoffs exist';
    END IF;
    IF EXISTS (SELECT FROM public.branch_activations) THEN
        RAISE EXCEPTION
            'Cannot remove legacy branch identities while tracked flows exist';
    END IF;
END;
$$;

DROP TABLE public.execution_outbox;
DROP TABLE public.branch_activations;

DROP TABLE IF EXISTS public.doccle_submissions;
DROP FUNCTION IF EXISTS public.doccle_submission_identity_guard();
DROP TYPE IF EXISTS public.doccle_submission_state;
DROP TYPE IF EXISTS public.doccle_submission_selector_mode;

COMMIT;
