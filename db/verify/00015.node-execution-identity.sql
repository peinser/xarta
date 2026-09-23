BEGIN;

SELECT trigger_outcome,
       trigger_outcome_event_public_id,
       trigger_originating_node_execution_id,
       trigger_originating_node_execution_public_id
FROM public.node_executions
WHERE FALSE;

SELECT 1 / COUNT(*)
FROM pg_constraint
WHERE conname = 'node_executions_trigger_identity_check'
  AND conrelid = 'public.node_executions'::regclass;

SELECT 1 / COUNT(*)
FROM pg_constraint
WHERE conrelid = 'public.node_executions'::regclass
  AND confrelid = 'public.node_executions'::regclass
  AND contype = 'f'
  AND pg_get_constraintdef(oid) LIKE
      'FOREIGN KEY (trigger_originating_node_execution_id) REFERENCES node_executions(_id)%';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'node_executions_trigger_originating_idx';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'node_executions_trigger_outcome_event_public_idx';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'node_executions_trigger_originating_public_idx';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'node_executions_root_flow_key'
  AND indexdef LIKE
      '%UNIQUE%WHERE (trigger_outcome_event_public_id IS NULL)%';

ROLLBACK;
