BEGIN;

SELECT provider_account_reference, reconciliation_lease_token,
       reconciliation_lease_until, reconciliation_next_at,
       reconciliation_attempts
FROM public.tracked_operations
WHERE FALSE;

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'tracked_operations_provider_identity_key';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'tracked_operations_reconciliation_idx'
  AND indexdef LIKE '%(reconciliation_next_at, updated_at, _id)%';

ROLLBACK;
