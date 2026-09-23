BEGIN;

DROP TABLE public.execution_outbox;
DROP TABLE public.branch_activations;
DROP TABLE public.provider_event_inbox;
ALTER TABLE public.node_executions
    DROP CONSTRAINT node_executions_trigger_outcome_event_fk;
DROP TABLE public.outcome_events;
DROP TABLE public.tracked_operations;
DROP TABLE public.execution_attempts;
DROP TABLE public.node_executions;
DROP TYPE public.tracked_operation_lifecycle;
DROP TYPE public.node_execution_state;

COMMIT;
