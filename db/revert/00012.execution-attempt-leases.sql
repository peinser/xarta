-- Revert xarta:00012.execution-attempt-leases from pg

BEGIN;

ALTER TABLE public.execution_attempts
    DROP CONSTRAINT execution_attempts_lease_state_check,
    DROP COLUMN lease_token,
    DROP COLUMN lease_until;

COMMIT;
