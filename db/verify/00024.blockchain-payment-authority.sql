BEGIN;

SELECT 1 / (COUNT(*) = 0)::integer
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
      'payment_price_quotes', 'payment_requirements', 'payment_purchases',
      'paid_intake_admissions', 'paid_preview_results', 'payment_outbox'
  );

SELECT 1 / (COUNT(*) = 0)::integer
FROM pg_type
WHERE typname IN (
    'payment_resource_type', 'payment_purchase_state', 'intake_admission_state'
);

SELECT 1 / (COUNT(*) = 1)::integer
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'node_executions'
  AND column_name = 'admission'
  AND data_type = 'jsonb'
  AND is_nullable = 'YES';

ROLLBACK;
