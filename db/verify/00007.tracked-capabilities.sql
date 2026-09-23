BEGIN;

SELECT _id, id, flow_id, node_id, state FROM public.node_executions WHERE FALSE;
SELECT _id, id, node_execution_id FROM public.execution_attempts WHERE FALSE;
SELECT _id, id, node_execution_id, lifecycle, version
FROM public.tracked_operations WHERE FALSE;
SELECT _id, id, node_execution_id, tracked_operation_id
FROM public.outcome_events WHERE FALSE;
SELECT _id, tracked_operation_id, adapter, external_event_id
FROM public.provider_event_inbox WHERE FALSE;
SELECT 'scheduled'::public.node_execution_state;
SELECT 'open'::public.tracked_operation_lifecycle;

SELECT 1 / CASE WHEN count(*) = 5 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE contype = 'p'
  AND conrelid IN (
      'public.node_executions'::regclass,
      'public.execution_attempts'::regclass,
      'public.tracked_operations'::regclass,
       'public.outcome_events'::regclass,
       'public.provider_event_inbox'::regclass
  )
  AND pg_get_constraintdef(oid) = 'PRIMARY KEY (_id)';

SELECT 1 / CASE WHEN count(*) = 4 THEN 1 ELSE 0 END
FROM pg_attribute
WHERE attrelid IN (
      'public.node_executions'::regclass,
      'public.execution_attempts'::regclass,
      'public.tracked_operations'::regclass,
      'public.outcome_events'::regclass
  )
  AND attname = 'id'
  AND attnotnull;

SELECT 1 / CASE WHEN count(*) = 4 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE contype = 'u'
  AND conrelid IN (
      'public.node_executions'::regclass,
      'public.execution_attempts'::regclass,
       'public.tracked_operations'::regclass,
       'public.outcome_events'::regclass
  )
  AND pg_get_constraintdef(oid) = 'UNIQUE (id)';

SELECT 1 / CASE WHEN count(*) = 6 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE contype = 'f'
  AND pg_get_constraintdef(oid) LIKE '%REFERENCES %(_id)%'
  AND conname IN (
      'execution_attempts_node_execution_id_fkey',
      'tracked_operations_node_execution_id_fkey',
      'outcome_events_node_execution_id_fkey',
      'outcome_events_tracked_operation_id_fkey',
       'node_executions_trigger_outcome_event_fk',
       'provider_event_inbox_tracked_operation_id_fkey'
  );

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'node_executions_root_flow_key'
  AND indexdef LIKE '%UNIQUE%';

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'outcome_events_operation_history_idx'
  AND indexdef LIKE '%(tracked_operation_id, received_at, _id)%';

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'outcome_events_synchronous_idempotency_idx'
  AND indexdef LIKE '%(node_execution_id, external_event_id)%';

ROLLBACK;
