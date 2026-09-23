BEGIN;

SELECT provisioning_lease_token, provisioning_lease_until, linked_receipt_order
FROM public.doccle_receivers
WHERE FALSE;

SELECT receipt_order
FROM public.doccle_receiver_callbacks
WHERE FALSE;

SELECT 1 / COUNT(*)
FROM pg_constraint
WHERE conname = 'doccle_receiver_provisioning_lease_check'
  AND conrelid = 'public.doccle_receivers'::regclass
  AND pg_get_constraintdef(oid) LIKE '%provisioning_lease_token IS NOT NULL%'
  AND pg_get_constraintdef(oid) LIKE '%provisioning_lease_until IS NOT NULL%';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'doccle_receivers_provisioning_lease_idx'
  AND indexdef LIKE '%(provisioning_lease_until)%'
  AND indexdef LIKE '%WHERE (state = ''provisioning''::doccle_receiver_state)%';

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'doccle_receiver_callbacks_receipt_order_idx'
  AND indexdef LIKE 'CREATE UNIQUE INDEX%';

SELECT 1 / (attidentity <> '')::integer
FROM pg_attribute
WHERE attrelid = 'public.doccle_receiver_callbacks'::regclass
  AND attname = 'receipt_order';

ROLLBACK;
