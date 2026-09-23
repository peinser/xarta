-- Deploy xarta:00012.execution-attempt-leases to pg

BEGIN;

ALTER TABLE public.execution_attempts
    ADD COLUMN lease_token uuid,
    ADD COLUMN lease_until timestamptz;

UPDATE public.execution_attempts
SET lease_token = gen_random_uuid(),
    lease_until = now() - interval '1 second'
WHERE completed_at IS NULL;

ALTER TABLE public.execution_attempts
    ADD CONSTRAINT execution_attempts_lease_state_check CHECK (
        (completed_at IS NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (completed_at IS NOT NULL AND lease_token IS NULL AND lease_until IS NULL)
    );

COMMIT;
