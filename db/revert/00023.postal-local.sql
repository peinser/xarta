-- Revert xarta:00023.postal-local from pg

BEGIN;

DROP SCHEMA postal_local CASCADE;

COMMIT;
