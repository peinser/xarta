BEGIN;

ALTER TABLE public.doccle_receivers
    ADD COLUMN provisioning_lease_token uuid,
    ADD COLUMN provisioning_lease_until timestamptz,
    ADD COLUMN linked_receipt_order bigint;

-- Ownership of in-flight requests from an older runtime cannot be proven.
-- Preserve that ambiguity and make the row explicitly reclaimable.
UPDATE public.doccle_receivers
SET state = 'uncertain',
    error = '{"category":"migration_recovery","message":"Previous provisioning owner was not durable"}'::jsonb
WHERE state = 'provisioning';

ALTER TABLE public.doccle_receivers
    ADD CONSTRAINT doccle_receiver_provisioning_lease_check CHECK (
        (
            state = 'provisioning'
            AND provisioning_lease_token IS NOT NULL
            AND provisioning_lease_until IS NOT NULL
        )
        OR
        (
            state <> 'provisioning'
            AND provisioning_lease_token IS NULL
            AND provisioning_lease_until IS NULL
        )
    );

CREATE INDEX doccle_receivers_provisioning_lease_idx
    ON public.doccle_receivers (provisioning_lease_until)
    WHERE state = 'provisioning';

ALTER TABLE public.doccle_receiver_callbacks
    DROP CONSTRAINT doccle_receiver_callbacks_destination_callback_identity_key,
    ADD COLUMN receipt_order bigint GENERATED ALWAYS AS IDENTITY;

CREATE UNIQUE INDEX doccle_receiver_callbacks_receipt_order_idx
    ON public.doccle_receiver_callbacks (receipt_order);

COMMIT;
