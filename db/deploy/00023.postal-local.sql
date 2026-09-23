-- Deploy xarta:00023.postal-local to pg

BEGIN;

CREATE SCHEMA postal_local;

CREATE TYPE postal_local.operation_state AS ENUM (
    'preparing', 'in_production', 'prepared', 'handed_over',
    'carrier_tracking', 'resolved', 'uncertain', 'failed', 'cancelled'
);
CREATE TYPE postal_local.assignment_state AS ENUM (
    'preparing', 'waiting_capacity', 'waiting_batch', 'waiting_stock',
    'pending', 'claimed', 'package_ready', 'package_acknowledged',
    'completed', 'cancelled'
);
CREATE TYPE postal_local.print_state AS ENUM (
    'pending', 'submitting', 'printing', 'completed', 'uncertain', 'failed'
);
CREATE TYPE postal_local.print_job_kind AS ENUM ('letter');
CREATE TYPE postal_local.physical_state AS ENUM (
    'waiting_for_print', 'ready_for_processing', 'processing',
    'ready_for_handover', 'handed_over'
);
CREATE TYPE postal_local.run_state AS ENUM (
    'claimed', 'building_package', 'package_ready',
    'package_acknowledged', 'completed', 'cancelled'
);
CREATE TYPE postal_local.batch_state AS ENUM (
    'open', 'completed', 'cancelled'
);
CREATE TYPE postal_local.deposit_state AS ENUM (
    'preparing', 'authorized', 'deposited', 'cancelled'
);
CREATE TYPE postal_local.capacity_reservation_state AS ENUM (
    'reserved', 'released'
);
CREATE TYPE postal_local.carrier_semantic_state AS ENUM (
    'announced', 'carrier_accepted', 'out_for_delivery',
    'available_for_pickup', 'delivery_exception', 'delivered', 'returned'
);
CREATE TYPE postal_local.proof_state AS ENUM (
    'pending', 'available', 'failed'
);

CREATE TABLE postal_local.operations (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    tracked_operation_id bigint NOT NULL UNIQUE
        REFERENCES public.tracked_operations(_id),
    service text NOT NULL CHECK (service IN ('ordinary', 'registered')),
    state postal_local.operation_state NOT NULL DEFAULT 'preparing',
    customer_reference text CHECK (customer_reference IS NULL OR customer_reference <> ''),
    provider_item_id text,
    provider_barcode text,
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (provider_item_id IS NULL OR provider_item_id <> ''),
    CHECK (provider_barcode IS NULL OR provider_barcode <> '')
);

CREATE TABLE postal_local.production_plans (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    operation_id bigint NOT NULL UNIQUE REFERENCES postal_local.operations(_id),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation_id, digest)
);

CREATE TABLE postal_local.production_tasks (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    operation_id bigint NOT NULL UNIQUE REFERENCES postal_local.operations(_id),
    production_plan_id bigint NOT NULL UNIQUE
        REFERENCES postal_local.production_plans(_id),
    assignment_state postal_local.assignment_state NOT NULL DEFAULT 'preparing',
    print_state postal_local.print_state NOT NULL DEFAULT 'pending',
    physical_state postal_local.physical_state NOT NULL DEFAULT 'waiting_for_print',
    generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    site_id text NOT NULL CHECK (site_id <> ''),
    service_date date,
    priority integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE postal_local.production_runs (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    station_id text NOT NULL CHECK (station_id <> ''),
    site_id text NOT NULL CHECK (site_id <> ''),
    claim_idempotency_key text NOT NULL CHECK (claim_idempotency_key <> ''),
    claim_token_digest text NOT NULL CHECK (claim_token_digest <> ''),
    state postal_local.run_state NOT NULL DEFAULT 'claimed',
    package_storage_reference text CHECK (
        package_storage_reference IS NULL OR package_storage_reference <> ''
    ),
    package_checksum text CHECK (
        package_checksum IS NULL OR package_checksum ~ '^[0-9a-f]{64}$'
    ),
    manifest_checksum text CHECK (
        manifest_checksum IS NULL OR manifest_checksum ~ '^[0-9a-f]{64}$'
    ),
    package_byte_count bigint CHECK (package_byte_count IS NULL OR package_byte_count >= 0),
    package_acknowledgement_key text,
    package_acknowledged_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (station_id, claim_idempotency_key),
    UNIQUE (station_id, package_acknowledgement_key),
    CHECK (package_acknowledgement_key IS NULL OR package_acknowledgement_key <> ''),
    CHECK (
        (package_acknowledgement_key IS NULL AND package_acknowledged_at IS NULL)
        OR
        (package_acknowledgement_key IS NOT NULL AND package_acknowledged_at IS NOT NULL)
    ),
    CHECK (
        (package_storage_reference IS NULL AND package_checksum IS NULL AND manifest_checksum IS NULL AND package_byte_count IS NULL)
        OR
        (package_storage_reference IS NOT NULL AND package_checksum IS NOT NULL AND manifest_checksum IS NOT NULL AND package_byte_count IS NOT NULL)
    )
);

CREATE TABLE postal_local.production_run_items (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    production_run_id bigint NOT NULL REFERENCES postal_local.production_runs(_id),
    production_task_id bigint NOT NULL REFERENCES postal_local.production_tasks(_id),
    generation integer NOT NULL CHECK (generation > 0),
    sequence integer NOT NULL CHECK (sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (production_run_id, sequence),
    UNIQUE (production_task_id, generation)
);

CREATE TABLE postal_local.artifacts (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    operation_id bigint NOT NULL REFERENCES postal_local.operations(_id),
    kind text NOT NULL CHECK (kind <> ''),
    generation integer NOT NULL DEFAULT 1 CHECK (generation > 0),
    storage_reference text NOT NULL CHECK (storage_reference <> ''),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size bigint NOT NULL CHECK (size >= 0),
    content_type text NOT NULL CHECK (content_type <> ''),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (operation_id, kind, generation),
    UNIQUE (storage_reference)
);

CREATE TABLE postal_local.print_jobs (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    production_run_item_id bigint NOT NULL
        REFERENCES postal_local.production_run_items(_id),
    sequence integer NOT NULL CHECK (sequence > 0),
    kind postal_local.print_job_kind NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    state postal_local.print_state NOT NULL DEFAULT 'pending',
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (production_run_item_id, kind, generation),
    UNIQUE (production_run_item_id, sequence)
);

CREATE TABLE postal_local.print_attempts (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    print_job_id bigint NOT NULL REFERENCES postal_local.print_jobs(_id),
    attempt integer NOT NULL CHECK (attempt > 0),
    state postal_local.print_state NOT NULL DEFAULT 'submitting',
    printer_job_name text NOT NULL UNIQUE CHECK (printer_job_name <> ''),
    station_event_identity text NOT NULL CHECK (station_event_identity <> ''),
    rendered_sha256 text NOT NULL CHECK (rendered_sha256 ~ '^[0-9a-f]{64}$'),
    rendered_byte_count bigint NOT NULL CHECK (rendered_byte_count > 0),
    printer_reference text,
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (print_job_id, attempt),
    UNIQUE (print_job_id, station_event_identity),
    CHECK (printer_reference IS NULL OR printer_reference <> '')
);

CREATE TABLE postal_local.handover_batches (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    site_id text NOT NULL CHECK (site_id <> ''),
    service_date date NOT NULL,
    state postal_local.batch_state NOT NULL DEFAULT 'open',
    operator_reference text,
    external_reference text,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (operator_reference IS NULL OR operator_reference <> ''),
    CHECK (external_reference IS NULL OR external_reference <> '')
);

CREATE TABLE postal_local.handover_batch_items (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    handover_batch_id bigint NOT NULL REFERENCES postal_local.handover_batches(_id),
    production_task_id bigint NOT NULL UNIQUE REFERENCES postal_local.production_tasks(_id),
    generation integer NOT NULL CHECK (generation > 0),
    sequence integer NOT NULL CHECK (sequence > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (handover_batch_id, sequence),
    UNIQUE (handover_batch_id, production_task_id)
);

CREATE TABLE postal_local.production_events (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    production_task_id bigint NOT NULL REFERENCES postal_local.production_tasks(_id),
    event_type text NOT NULL CHECK (
        event_type IN ('start_production', 'ready_for_handover', 'confirm_handover')
    ),
    generation integer NOT NULL CHECK (generation > 0),
    handover_batch_id bigint REFERENCES postal_local.handover_batches(_id),
    station_id text NOT NULL CHECK (station_id <> ''),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (event_type = 'confirm_handover' AND handover_batch_id IS NOT NULL)
        OR (event_type <> 'confirm_handover' AND handover_batch_id IS NULL)
    )
);

CREATE TABLE postal_local.port_paid_deposit_batches (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    site_id text NOT NULL CHECK (site_id <> ''),
    service_date date NOT NULL,
    state postal_local.deposit_state NOT NULL DEFAULT 'preparing',
    service text NOT NULL CHECK (service <> ''),
    speed text NOT NULL CHECK (speed <> ''),
    postal_format text NOT NULL CHECK (postal_format <> ''),
    weight_band text NOT NULL CHECK (weight_band <> ''),
    profile_id text NOT NULL CHECK (profile_id <> ''),
    profile_revision text NOT NULL CHECK (profile_revision <> ''),
    channel text NOT NULL CHECK (channel <> ''),
    external_reference text,
    operator_reference text,
    authorization_artifact_id bigint REFERENCES postal_local.artifacts(_id),
    deposited_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (external_reference IS NULL OR external_reference <> ''),
    CHECK (operator_reference IS NULL OR operator_reference <> '')
);

CREATE TABLE postal_local.port_paid_deposit_batch_items (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    port_paid_deposit_batch_id bigint NOT NULL
        REFERENCES postal_local.port_paid_deposit_batches(_id),
    production_task_id bigint NOT NULL UNIQUE REFERENCES postal_local.production_tasks(_id),
    generation integer NOT NULL CHECK (generation > 0),
    sequence integer NOT NULL CHECK (sequence > 0),
    declared_weight_g numeric NOT NULL CHECK (declared_weight_g >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (port_paid_deposit_batch_id, sequence),
    UNIQUE (port_paid_deposit_batch_id, production_task_id)
);

CREATE TABLE postal_local.capacity_days (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    pool_id text NOT NULL CHECK (pool_id <> ''),
    service_date date NOT NULL,
    timezone text NOT NULL CHECK (timezone <> ''),
    daily_admission_limit integer NOT NULL CHECK (daily_admission_limit >= -1),
    admitted integer NOT NULL DEFAULT 0 CHECK (admitted >= 0),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (pool_id, service_date),
    CHECK (daily_admission_limit = -1 OR admitted <= daily_admission_limit)
);

CREATE TABLE postal_local.capacity_reservations (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    capacity_day_id bigint NOT NULL REFERENCES postal_local.capacity_days(_id),
    production_task_id bigint NOT NULL UNIQUE REFERENCES postal_local.production_tasks(_id),
    state postal_local.capacity_reservation_state NOT NULL DEFAULT 'reserved',
    quantity integer NOT NULL DEFAULT 1 CHECK (quantity = 1),
    reserved_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    CHECK (
        (state = 'reserved' AND released_at IS NULL)
        OR (state = 'released' AND released_at IS NOT NULL)
    )
);

CREATE TABLE postal_local.carrier_observations (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    production_task_id bigint NOT NULL REFERENCES postal_local.production_tasks(_id),
    provider text NOT NULL CHECK (provider <> ''),
    external_event_id text NOT NULL CHECK (external_event_id <> ''),
    provider_state text NOT NULL CHECK (provider_state <> ''),
    semantic_state postal_local.carrier_semantic_state,
    requires_reconciliation boolean NOT NULL DEFAULT false,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    observed_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (production_task_id, provider, external_event_id)
);

CREATE TABLE postal_local.carrier_proofs (
    _id bigserial PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    production_task_id bigint NOT NULL REFERENCES postal_local.production_tasks(_id),
    kind text NOT NULL CHECK (kind <> ''),
    state postal_local.proof_state NOT NULL DEFAULT 'pending',
    provider_proof_id text,
    artifact_id bigint REFERENCES postal_local.artifacts(_id),
    error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (production_task_id, kind),
    UNIQUE (provider_proof_id),
    CHECK (provider_proof_id IS NULL OR provider_proof_id <> ''),
    CHECK (
        (state = 'available' AND artifact_id IS NOT NULL)
        OR (state <> 'available' AND artifact_id IS NULL)
    )
);

CREATE UNIQUE INDEX production_events_without_batch_identity_idx
    ON postal_local.production_events (production_task_id, event_type, generation)
    WHERE handover_batch_id IS NULL;
CREATE UNIQUE INDEX production_events_with_batch_identity_idx
    ON postal_local.production_events (
        production_task_id, event_type, generation, handover_batch_id
    ) WHERE handover_batch_id IS NOT NULL;

CREATE INDEX production_tasks_queue_idx
    ON postal_local.production_tasks (site_id, service_date, priority DESC, _id)
    WHERE assignment_state = 'pending';
CREATE INDEX production_run_items_run_idx
    ON postal_local.production_run_items (production_run_id, sequence);
CREATE INDEX print_jobs_run_item_idx
    ON postal_local.print_jobs (production_run_item_id, sequence);
CREATE INDEX print_jobs_pending_idx
    ON postal_local.print_jobs (state, _id)
    WHERE state IN ('pending', 'submitting', 'printing', 'uncertain');
CREATE INDEX print_attempts_job_idx
    ON postal_local.print_attempts (print_job_id, attempt);
CREATE INDEX handover_batch_items_batch_idx
    ON postal_local.handover_batch_items (handover_batch_id, sequence);
CREATE INDEX production_events_task_history_idx
    ON postal_local.production_events (production_task_id, received_at, _id);
CREATE INDEX production_events_handover_batch_idx
    ON postal_local.production_events (handover_batch_id)
    WHERE handover_batch_id IS NOT NULL;
CREATE INDEX port_paid_deposit_batch_items_batch_idx
    ON postal_local.port_paid_deposit_batch_items (
        port_paid_deposit_batch_id, sequence
    );
CREATE INDEX capacity_reservations_day_idx
    ON postal_local.capacity_reservations (capacity_day_id)
    WHERE state = 'reserved';
CREATE INDEX carrier_observations_task_history_idx
    ON postal_local.carrier_observations (production_task_id, received_at, _id);
CREATE INDEX carrier_observations_reconciliation_idx
    ON postal_local.carrier_observations (received_at, _id)
    WHERE requires_reconciliation;
CREATE INDEX carrier_proofs_artifact_idx
    ON postal_local.carrier_proofs (artifact_id)
    WHERE artifact_id IS NOT NULL;
CREATE INDEX port_paid_deposit_batches_authorization_artifact_idx
    ON postal_local.port_paid_deposit_batches (authorization_artifact_id)
    WHERE authorization_artifact_id IS NOT NULL;

CREATE FUNCTION postal_local.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER production_plans_immutable
BEFORE UPDATE OR DELETE ON postal_local.production_plans
FOR EACH ROW EXECUTE FUNCTION postal_local.reject_mutation();

CREATE TRIGGER artifacts_immutable
BEFORE UPDATE OR DELETE ON postal_local.artifacts
FOR EACH ROW EXECUTE FUNCTION postal_local.reject_mutation();

CREATE TRIGGER production_events_append_only
BEFORE UPDATE OR DELETE ON postal_local.production_events
FOR EACH ROW EXECUTE FUNCTION postal_local.reject_mutation();

CREATE TRIGGER carrier_observations_append_only
BEFORE UPDATE OR DELETE ON postal_local.carrier_observations
FOR EACH ROW EXECUTE FUNCTION postal_local.reject_mutation();

COMMIT;
