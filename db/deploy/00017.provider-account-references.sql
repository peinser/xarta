BEGIN;

ALTER TABLE public.tracked_operations
    ADD COLUMN provider_account_reference text,
    ADD COLUMN reconciliation_lease_token uuid,
    ADD COLUMN reconciliation_lease_until timestamptz,
    ADD COLUMN reconciliation_next_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN reconciliation_attempts integer NOT NULL DEFAULT 0
        CHECK (reconciliation_attempts >= 0);

-- A tenant ID cannot be reconstructed safely from the UBL sender alone. Refuse
-- to silently orphan callbacks for pre-migration active Peppol operations.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.tracked_operations
        WHERE capability = 'peppol'
          AND provider_reference IS NOT NULL
          AND lifecycle IN ('open', 'uncertain')
    ) THEN
        RAISE EXCEPTION
            'Active Peppol operations require provider_account_reference backfill before migration 00017';
    END IF;
END;
$$;

CREATE UNIQUE INDEX tracked_operations_provider_identity_key
    ON public.tracked_operations (
        adapter, provider_account_reference, provider_reference
    ) NULLS NOT DISTINCT
    WHERE provider_reference IS NOT NULL;

CREATE INDEX tracked_operations_reconciliation_idx
    ON public.tracked_operations (reconciliation_next_at, updated_at, _id)
    WHERE capability = 'peppol'
      AND provider_reference IS NOT NULL
      AND lifecycle IN ('open', 'uncertain');

COMMIT;
