BEGIN;

SELECT correlation_id,
       trigger_outcome_event_public_id,
       trigger_originating_node_execution_public_id
FROM public.node_executions
WHERE FALSE;

SELECT 1 / (
    CASE WHEN to_regclass('public.execution_outbox') IS NULL THEN 1 ELSE 0 END
);
SELECT 1 / (
    CASE WHEN to_regclass('public.branch_activations') IS NULL THEN 1 ELSE 0 END
);
SELECT 1 / (
    CASE WHEN to_regclass('public.doccle_submissions') IS NULL THEN 1 ELSE 0 END
);

ROLLBACK;
