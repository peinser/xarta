-- Verify xarta:00012.execution-attempt-leases on pg

BEGIN;

SELECT lease_token, lease_until
FROM public.execution_attempts
WHERE FALSE;

SELECT 1 / CASE WHEN count(*) = 1 THEN 1 ELSE 0 END
FROM pg_constraint
WHERE conrelid = 'public.execution_attempts'::regclass
  AND conname = 'execution_attempts_lease_state_check'
  AND contype = 'c';

ROLLBACK;
