BEGIN;

DROP TABLE public.payment_outbox;
DROP TABLE public.paid_preview_results;
DROP TABLE public.paid_intake_admissions;
DROP TABLE public.payment_purchases;
DROP TABLE public.payment_requirements;
DROP TABLE public.payment_price_quotes;
DROP TYPE public.intake_admission_state;
DROP TYPE public.payment_purchase_state;
DROP TYPE public.payment_resource_type;

ALTER TABLE public.node_executions
    ADD COLUMN admission jsonb;

COMMIT;
