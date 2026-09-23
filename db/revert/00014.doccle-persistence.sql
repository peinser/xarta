-- Revert xarta:00014.doccle-persistence from pg

BEGIN;

DROP TABLE IF EXISTS public.doccle_submissions;
DROP TABLE public.doccle_receiver_callbacks;
DROP TABLE public.doccle_receivers;
DROP FUNCTION IF EXISTS public.doccle_submission_identity_guard();
DROP FUNCTION public.doccle_receiver_identity_guard();
DROP TYPE IF EXISTS public.doccle_submission_state;
DROP TYPE IF EXISTS public.doccle_submission_selector_mode;
DROP TYPE public.doccle_receiver_state;

COMMIT;
