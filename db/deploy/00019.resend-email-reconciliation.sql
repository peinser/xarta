CREATE INDEX CONCURRENTLY tracked_operations_resend_reconciliation_idx
    ON public.tracked_operations (reconciliation_next_at, updated_at, _id)
    WHERE capability = 'email'
      AND adapter = 'resend-rest'
      AND provider_reference IS NOT NULL
      AND lifecycle IN ('open', 'uncertain');
