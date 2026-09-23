-- Verify xarta:00023.postal-local on pg

BEGIN;

SELECT 1 / (to_regnamespace('postal_local') IS NOT NULL)::integer;

SELECT 1 / (COUNT(*) = 17)::integer
FROM pg_tables
WHERE schemaname = 'postal_local'
  AND tablename IN (
      'operations', 'production_plans', 'production_tasks', 'production_runs',
      'production_run_items', 'print_jobs', 'print_attempts',
      'production_events', 'handover_batches', 'handover_batch_items',
      'port_paid_deposit_batches', 'port_paid_deposit_batch_items',
      'capacity_days', 'capacity_reservations', 'artifacts',
      'carrier_observations', 'carrier_proofs'
  );

SELECT 1 / (enum_range(NULL::postal_local.assignment_state)::text[] = ARRAY[
    'preparing', 'waiting_capacity', 'waiting_batch', 'waiting_stock',
    'pending', 'claimed', 'package_ready', 'package_acknowledged',
    'completed', 'cancelled'
])::integer;
SELECT 1 / (enum_range(NULL::postal_local.print_state)::text[] = ARRAY[
    'pending', 'submitting', 'printing', 'completed', 'uncertain', 'failed'
])::integer;
SELECT 1 / (enum_range(NULL::postal_local.print_job_kind)::text[] = ARRAY[
    'letter'
])::integer;
SELECT 1 / (enum_range(NULL::postal_local.physical_state)::text[] = ARRAY[
    'waiting_for_print', 'ready_for_processing', 'processing',
    'ready_for_handover', 'handed_over'
])::integer;
SELECT 1 / (enum_range(NULL::postal_local.carrier_semantic_state)::text[] = ARRAY[
    'announced', 'carrier_accepted', 'out_for_delivery',
    'available_for_pickup', 'delivery_exception', 'delivered', 'returned'
])::integer;

-- Every core table uses a private bigint primary key and a required public UUID.
SELECT 1 / (COUNT(*) = 17)::integer
FROM pg_constraint AS c
JOIN pg_attribute AS a
  ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.connamespace = 'postal_local'::regnamespace
  AND c.contype = 'p'
  AND c.conkey = ARRAY[a.attnum]::smallint[]
  AND a.attname = '_id'
  AND a.atttypid = 'bigint'::regtype;

SELECT 1 / (COUNT(*) = 17)::integer
FROM pg_attribute AS a
JOIN pg_class AS table_class ON table_class.oid = a.attrelid
WHERE table_class.relnamespace = 'postal_local'::regnamespace
  AND table_class.relkind = 'r'
  AND a.attname = 'id'
  AND a.atttypid = 'uuid'::regtype
  AND a.attnotnull
  AND EXISTS (
      SELECT 1
      FROM pg_constraint AS c
      WHERE c.conrelid = a.attrelid
        AND c.contype = 'u'
        AND c.conkey = ARRAY[a.attnum]::smallint[]
  );

SELECT 1 / (
    (
        SELECT atttypid
        FROM pg_attribute
        WHERE attrelid = 'postal_local.print_jobs'::regclass
          AND attname = 'kind'
          AND NOT attisdropped
    ) = 'postal_local.print_job_kind'::regtype
)::integer;

SELECT 1 / (COUNT(*) = 4)::integer
FROM pg_attribute
WHERE attrelid IN (
    'postal_local.production_runs'::regclass,
    'postal_local.print_attempts'::regclass
)
  AND attname IN (
      'package_storage_reference', 'package_byte_count',
      'rendered_sha256', 'rendered_byte_count'
  )
  AND attnotnull = (attname IN ('rendered_sha256', 'rendered_byte_count'))
  AND NOT attisdropped;

SELECT 1 / (COUNT(*) = 0)::integer
FROM pg_attribute
WHERE attrelid IN (
    'postal_local.production_runs'::regclass,
    'postal_local.print_jobs'::regclass
)
  AND attname IN ('package_artifact_id', 'source_artifact_id')
  AND NOT attisdropped;

-- Verify every intended FK edge, including the same-database tracking boundary.
WITH expected(source_table, source_column, target_table) AS (
    VALUES
      ('operations', 'tracked_operation_id', 'public.tracked_operations'),
      ('production_plans', 'operation_id', 'postal_local.operations'),
      ('production_tasks', 'operation_id', 'postal_local.operations'),
      ('production_tasks', 'production_plan_id', 'postal_local.production_plans'),
      ('production_run_items', 'production_run_id', 'postal_local.production_runs'),
      ('production_run_items', 'production_task_id', 'postal_local.production_tasks'),
      ('artifacts', 'operation_id', 'postal_local.operations'),
      ('print_jobs', 'production_run_item_id', 'postal_local.production_run_items'),
      ('print_attempts', 'print_job_id', 'postal_local.print_jobs'),
      ('handover_batch_items', 'handover_batch_id', 'postal_local.handover_batches'),
      ('handover_batch_items', 'production_task_id', 'postal_local.production_tasks'),
      ('production_events', 'production_task_id', 'postal_local.production_tasks'),
      ('production_events', 'handover_batch_id', 'postal_local.handover_batches'),
      ('port_paid_deposit_batches', 'authorization_artifact_id', 'postal_local.artifacts'),
      ('port_paid_deposit_batch_items', 'port_paid_deposit_batch_id', 'postal_local.port_paid_deposit_batches'),
      ('port_paid_deposit_batch_items', 'production_task_id', 'postal_local.production_tasks'),
      ('capacity_reservations', 'capacity_day_id', 'postal_local.capacity_days'),
      ('capacity_reservations', 'production_task_id', 'postal_local.production_tasks'),
      ('carrier_observations', 'production_task_id', 'postal_local.production_tasks'),
      ('carrier_proofs', 'production_task_id', 'postal_local.production_tasks'),
      ('carrier_proofs', 'artifact_id', 'postal_local.artifacts')
), actual AS (
    SELECT source.relname AS source_table,
           source_attribute.attname AS source_column,
           target_namespace.nspname || '.' || target.relname AS target_table
    FROM pg_constraint AS c
    JOIN pg_class AS source ON source.oid = c.conrelid
    JOIN pg_attribute AS source_attribute
      ON source_attribute.attrelid = c.conrelid
     AND source_attribute.attnum = c.conkey[1]
    JOIN pg_class AS target ON target.oid = c.confrelid
    JOIN pg_namespace AS target_namespace ON target_namespace.oid = target.relnamespace
    WHERE c.connamespace = 'postal_local'::regnamespace
      AND c.contype = 'f'
      AND cardinality(c.conkey) = 1
)
SELECT 1 / (
    NOT EXISTS (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    AND NOT EXISTS (SELECT * FROM actual EXCEPT SELECT * FROM expected)
)::integer;

-- No foreign-key lookup should require a full scan of its source table.
SELECT 1 / (COUNT(*) = 0)::integer
FROM pg_constraint AS foreign_key
WHERE foreign_key.contype = 'f'
  AND foreign_key.connamespace = 'postal_local'::regnamespace
  AND NOT EXISTS (
      SELECT 1
      FROM pg_index AS idx
      WHERE idx.indrelid = foreign_key.conrelid
        AND idx.indisvalid
        AND foreign_key.conkey <@ idx.indkey::smallint[]
  );

SELECT 1 / (COUNT(*) = 4)::integer
FROM pg_trigger AS trigger
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
WHERE trigger.tgrelid IN (
        'postal_local.production_plans'::regclass,
        'postal_local.artifacts'::regclass,
        'postal_local.production_events'::regclass,
        'postal_local.carrier_observations'::regclass
    )
  AND trigger.tgname IN (
      'production_plans_immutable', 'artifacts_immutable',
      'production_events_append_only', 'carrier_observations_append_only'
  )
  AND function.oid = 'postal_local.reject_mutation()'::regprocedure
  AND NOT trigger.tgisinternal;

SELECT 1 / (COUNT(*) = 2)::integer
FROM pg_indexes
WHERE schemaname = 'postal_local'
  AND indexname IN (
      'production_events_without_batch_identity_idx',
      'production_events_with_batch_identity_idx'
  )
  AND indexdef LIKE 'CREATE UNIQUE INDEX%';

SELECT 1 / (COUNT(*) = 7)::integer
FROM pg_constraint
WHERE connamespace = 'postal_local'::regnamespace
  AND contype = 'c'
  AND (
      (conrelid = 'postal_local.production_tasks'::regclass
       AND pg_get_constraintdef(oid) LIKE '%generation > 0%')
      OR (conrelid = 'postal_local.production_runs'::regclass
          AND pg_get_constraintdef(oid) LIKE '%package_acknowledgement_key%package_acknowledged_at%')
      OR (conrelid = 'postal_local.production_events'::regclass
          AND pg_get_constraintdef(oid) LIKE '%confirm_handover%handover_batch_id%')
      OR (conrelid = 'postal_local.capacity_days'::regclass
          AND pg_get_constraintdef(oid) LIKE '%daily_admission_limit%admitted <= daily_admission_limit%')
      OR (conrelid = 'postal_local.capacity_reservations'::regclass
          AND pg_get_constraintdef(oid) LIKE '%quantity = 1%')
      OR (conrelid = 'postal_local.capacity_reservations'::regclass
          AND pg_get_constraintdef(oid) LIKE '%released_at%')
      OR (conrelid = 'postal_local.carrier_proofs'::regclass
          AND pg_get_constraintdef(oid) LIKE '%available%artifact_id%')
  );

SELECT 1 / (COUNT(*) = 5)::integer
FROM pg_constraint
WHERE connamespace = 'postal_local'::regnamespace
  AND contype = 'u'
  AND (
      (conrelid = 'postal_local.operations'::regclass
       AND pg_get_constraintdef(oid) LIKE '%tracked_operation_id%')
      OR (conrelid = 'postal_local.production_runs'::regclass
          AND pg_get_constraintdef(oid) LIKE '%station_id, claim_idempotency_key%')
      OR (conrelid = 'postal_local.production_runs'::regclass
          AND pg_get_constraintdef(oid) LIKE '%station_id, package_acknowledgement_key%')
      OR (conrelid = 'postal_local.print_attempts'::regclass
          AND pg_get_constraintdef(oid) LIKE '%print_job_id, attempt%')
      OR (conrelid = 'postal_local.carrier_observations'::regclass
          AND pg_get_constraintdef(oid) LIKE '%production_task_id, provider, external_event_id%')
  );

ROLLBACK;
