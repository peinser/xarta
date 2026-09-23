BEGIN;

DROP INDEX public.doccle_receiver_callbacks_receipt_order_idx;
ALTER TABLE public.doccle_receiver_callbacks
    DROP COLUMN receipt_order,
    ADD CONSTRAINT doccle_receiver_callbacks_destination_callback_identity_key
        UNIQUE (destination, callback_identity);

DROP INDEX public.doccle_receivers_provisioning_lease_idx;
ALTER TABLE public.doccle_receivers
    DROP CONSTRAINT doccle_receiver_provisioning_lease_check,
    DROP COLUMN linked_receipt_order,
    DROP COLUMN provisioning_lease_until,
    DROP COLUMN provisioning_lease_token;

COMMIT;
