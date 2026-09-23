BEGIN;

SELECT 1 / (COUNT(*) = 6)::integer
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
      'payment_price_quotes', 'payment_requirements', 'payment_purchases',
      'paid_intake_admissions', 'paid_preview_results', 'payment_outbox'
  );

SELECT 1 / (enum_range(NULL::public.payment_resource_type)::text[] =
    ARRAY['intake', 'preview'])::integer;
SELECT 1 / (enum_range(NULL::public.payment_purchase_state)::text[] =
    ARRAY['dormant', 'settling', 'uncertain', 'completed', 'failed'])::integer;
SELECT 1 / (enum_range(NULL::public.intake_admission_state)::text[] =
    ARRAY['dormant', 'active'])::integer;

SELECT 1 / (COUNT(*) = 6)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS attribute
  ON attribute.attrelid = c.conrelid
 AND attribute.attnum = c.conkey[1]
WHERE c.conrelid IN (
        'public.payment_price_quotes'::regclass,
        'public.payment_requirements'::regclass,
        'public.payment_purchases'::regclass,
        'public.paid_intake_admissions'::regclass,
        'public.paid_preview_results'::regclass,
        'public.payment_outbox'::regclass
    )
  AND c.contype = 'p'
  AND c.conkey = ARRAY[attribute.attnum]::smallint[]
  AND attribute.attname = '_id'
  AND attribute.atttypid = 'bigint'::regtype;

SELECT 1 / (COUNT(*) = 5)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS source
  ON source.attrelid = c.conrelid
 AND source.attnum = c.conkey[1]
JOIN pg_attribute AS target
  ON target.attrelid = c.confrelid
 AND target.attnum = c.confkey[1]
WHERE c.conrelid IN (
        'public.payment_requirements'::regclass,
        'public.payment_purchases'::regclass,
        'public.paid_intake_admissions'::regclass,
        'public.paid_preview_results'::regclass,
        'public.payment_outbox'::regclass
    )
  AND c.contype = 'f'
  AND source.atttypid = 'bigint'::regtype
  AND target.attname = '_id';

SELECT 1 / (COUNT(*) = 3)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS attribute
  ON attribute.attrelid = c.conrelid
 AND attribute.attnum = c.conkey[1]
WHERE c.contype = 'u'
  AND c.conkey = ARRAY[attribute.attnum]::smallint[]
  AND (c.conrelid, attribute.attname) IN (
      ('public.payment_price_quotes'::regclass, 'id'),
      ('public.payment_purchases'::regclass, 'payment_id'),
      ('public.payment_outbox'::regclass, 'id')
  );

SELECT 1 / (COUNT(*) = 3)::integer
FROM pg_attribute
WHERE (attrelid, attname) IN (
        ('public.payment_price_quotes'::regclass, 'id'),
        ('public.payment_purchases'::regclass, 'payment_id'),
        ('public.payment_outbox'::regclass, 'id')
    )
  AND attnotnull;

SELECT 1 / (COUNT(*) = 0)::integer
FROM pg_constraint AS foreign_key
WHERE foreign_key.contype = 'f'
  AND foreign_key.conrelid IN (
      'public.payment_requirements'::regclass,
      'public.payment_purchases'::regclass,
      'public.paid_intake_admissions'::regclass,
      'public.paid_preview_results'::regclass,
      'public.payment_outbox'::regclass
  )
  AND NOT EXISTS (
      SELECT 1 FROM pg_index AS idx
      WHERE idx.indrelid = foreign_key.conrelid
        AND foreign_key.conkey <@ idx.indkey::smallint[]
  );

SELECT 1 / COUNT(*)
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname = 'payment_outbox_pending_idx'
  AND indexdef LIKE '%(available_at, id)%'
  AND indexdef LIKE '%WHERE ((published_at IS NULL) AND (available_at IS NOT NULL))%';

ROLLBACK;
