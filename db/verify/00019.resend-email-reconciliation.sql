BEGIN;

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'tracked_operations_resend_reconciliation_idx'
  AND indexdef LIKE '%(reconciliation_next_at, updated_at, _id)%'
  AND indexdef LIKE '%capability = ''email''%'
  AND indexdef LIKE '%adapter = ''resend-rest''%'
  AND indexdef LIKE '%provider_reference IS NOT NULL%';

ROLLBACK;
