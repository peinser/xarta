-- Verify xarta:00014.doccle-persistence on pg

BEGIN;

SELECT _id, id, destination, subject, external_receiver_id, state, linked, error
FROM public.doccle_receivers WHERE FALSE;

SELECT _id, id, destination, callback_identity, receiver_id,
       external_receiver_id, linked, payload, applied
FROM public.doccle_receiver_callbacks WHERE FALSE;

SELECT 1 / (to_regclass('public.doccle_submissions') IS NULL)::integer;

SELECT 1 / (enum_range(NULL::public.doccle_receiver_state)::text[] =
    ARRAY['pending', 'provisioning', 'provisioned', 'uncertain', 'failed'])::integer;

SELECT 1 / (COUNT(*) = 2)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS attribute
  ON attribute.attrelid = c.conrelid
 AND attribute.attnum = c.conkey[1]
WHERE c.conrelid IN (
        'public.doccle_receivers'::regclass,
        'public.doccle_receiver_callbacks'::regclass
    )
  AND c.contype = 'p'
  AND c.conkey = ARRAY[attribute.attnum]::smallint[]
  AND attribute.attname = '_id'
  AND attribute.atttypid = 'bigint'::regtype;

SELECT 1 / (COUNT(*) = 1)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS target
  ON target.attrelid = c.confrelid
 AND target.attnum = c.confkey[1]
JOIN pg_attribute AS source
  ON source.attrelid = c.conrelid
 AND source.attnum = c.conkey[1]
WHERE c.conrelid = 'public.doccle_receiver_callbacks'::regclass
  AND c.contype = 'f'
  AND source.atttypid = 'bigint'::regtype
  AND target.attname = '_id';

SELECT 1 / (COUNT(*) = 1)::integer
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
      'doccle_receiver_callbacks_receiver_idx'
  );

SELECT 1 / (COUNT(*) = 2)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS attribute
  ON attribute.attrelid = c.conrelid
 AND attribute.attnum = c.conkey[1]
WHERE c.contype = 'u'
  AND c.conkey = ARRAY[attribute.attnum]::smallint[]
  AND (c.conrelid, attribute.attname) IN (
      ('public.doccle_receivers'::regclass, 'id'),
      ('public.doccle_receiver_callbacks'::regclass, 'id')
  );

SELECT 1 / (COUNT(*) = 2)::integer
FROM pg_attribute
WHERE (attrelid, attname) IN (
        ('public.doccle_receivers'::regclass, 'id'),
        ('public.doccle_receiver_callbacks'::regclass, 'id')
    )
  AND attnotnull;

SELECT 1 / (COUNT(*) = 1)::integer
FROM pg_trigger AS t
JOIN pg_proc AS p ON p.oid = t.tgfoid
WHERE tgname = 'doccle_receiver_identity_guard'
  AND NOT tgisinternal
  AND pg_get_functiondef(p.oid) LIKE '%NEW._id%'
  AND pg_get_functiondef(p.oid) LIKE '%OLD._id%';

ROLLBACK;
