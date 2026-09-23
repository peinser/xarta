BEGIN;

DROP INDEX IF EXISTS public.node_executions_trigger_originating_public_idx;
DROP INDEX IF EXISTS public.node_executions_trigger_outcome_event_public_idx;
DROP INDEX public.node_executions_trigger_originating_idx;
DROP INDEX public.node_executions_root_flow_key;
CREATE UNIQUE INDEX node_executions_root_flow_key
    ON public.node_executions (flow_id)
    WHERE trigger_outcome_event_id IS NULL;
ALTER TABLE public.node_executions
    DROP CONSTRAINT node_executions_trigger_identity_check,
    DROP COLUMN IF EXISTS trigger_originating_node_execution_public_id,
    DROP COLUMN trigger_originating_node_execution_id,
    DROP COLUMN IF EXISTS trigger_outcome_event_public_id,
    DROP COLUMN trigger_outcome;

COMMIT;
