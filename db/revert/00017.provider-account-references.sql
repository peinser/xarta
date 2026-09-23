BEGIN;

DROP INDEX public.tracked_operations_provider_identity_key;
DROP INDEX public.tracked_operations_reconciliation_idx;
ALTER TABLE public.tracked_operations
    DROP COLUMN provider_account_reference,
    DROP COLUMN reconciliation_lease_token,
    DROP COLUMN reconciliation_lease_until,
    DROP COLUMN reconciliation_next_at,
    DROP COLUMN reconciliation_attempts;

COMMIT;
