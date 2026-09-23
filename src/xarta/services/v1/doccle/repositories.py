from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

import orjson

from xarta.services.v1.doccle.models import CallbackResult
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import ReceiverCallback
from xarta.services.v1.doccle.models import ReceiverState

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    import asyncpg  # type: ignore[import-untyped]


def _json(value: Any) -> str:
    return orjson.dumps(value, default=str).decode()


def _decoded(value: Any) -> Any:
    return orjson.loads(value) if isinstance(value, str) else value


def _receiver(row: asyncpg.Record) -> DoccleReceiver:
    return DoccleReceiver(
        id=row["id"],
        destination=row["destination"],
        subject=dict(_decoded(row["subject"])),
        external_receiver_id=row["external_receiver_id"],
        state=ReceiverState(row["state"]),
        linked=row["linked"],
        error=_decoded(row["error"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        provisioning_lease_token=row.get("provisioning_lease_token"),
        provisioning_lease_until=row.get("provisioning_lease_until"),
        linked_receipt_order=row.get("linked_receipt_order"),
    )


def _callback(row: asyncpg.Record) -> ReceiverCallback:
    return ReceiverCallback(
        id=row["id"],
        destination=row["destination"],
        callback_identity=row["callback_identity"],
        receiver_id=row["receiver_id"],
        external_receiver_id=row["external_receiver_id"],
        linked=row["linked"],
        payload=_decoded(row["payload"]),
        applied=row["applied"],
        received_at=row["received_at"],
        receipt_order=row.get("receipt_order", 0),
    )


class ReceiverRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def load(self, receiver_id: UUID) -> DoccleReceiver | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM doccle_receivers WHERE id = $1", receiver_id
            )
        return _receiver(row) if row is not None else None

    async def load_by_subject(
        self, destination: str, subject: dict[str, Any]
    ) -> DoccleReceiver | None:
        _validate_subject(subject)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM doccle_receivers "
                "WHERE destination = $1 AND subject = $2::jsonb",
                destination,
                _json(subject),
            )
        return _receiver(row) if row is not None else None

    async def load_by_external_id(
        self, destination: str, external_receiver_id: str
    ) -> DoccleReceiver | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM doccle_receivers "
                "WHERE destination = $1 AND external_receiver_id = $2",
                destination,
                external_receiver_id,
            )
        return _receiver(row) if row is not None else None

    async def insert_or_load(
        self,
        destination: str,
        subject: dict[str, Any],
        *,
        receiver_id: UUID | None = None,
        external_receiver_id: str | None = None,
    ) -> DoccleReceiver:
        if not isinstance(destination, str) or not destination:
            raise ValueError("Doccle receiver destination must not be empty")
        _validate_subject(subject)
        # Both identities are allocated before the first provider request. The no-op
        # update makes a conflicting concurrent INSERT return the committed winner.
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO doccle_receivers (
                    id, destination, subject, external_receiver_id, state
                ) VALUES ($1, $2, $3::jsonb, $4, 'pending')
                ON CONFLICT (destination, subject) DO UPDATE
                SET subject = EXCLUDED.subject
                RETURNING *
                """,
                receiver_id or uuid4(),
                destination,
                _json(subject),
                external_receiver_id or str(uuid4()),
            )
        return _receiver(row)

    async def claim_provisioning(
        self, receiver_id: UUID, *, lease_seconds: float
    ) -> DoccleReceiver | None:
        if lease_seconds <= 0:
            raise ValueError("Doccle provisioning lease must be greater than zero")
        lease_token = uuid4()
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE doccle_receivers
                SET state = 'provisioning', provisioning_lease_token = $2,
                    provisioning_lease_until = now() + ($3::double precision * interval '1 second'),
                    error = NULL, updated_at = now()
                WHERE id = $1
                  AND (
                    state = ANY($4::doccle_receiver_state[])
                    OR (state = 'provisioning' AND provisioning_lease_until <= now())
                  )
                RETURNING *
                """,
                receiver_id,
                lease_token,
                lease_seconds,
                [
                    ReceiverState.PENDING.value,
                    ReceiverState.FAILED.value,
                    ReceiverState.UNCERTAIN.value,
                ],
            )
        return _receiver(row) if row is not None else None

    async def complete_provisioning(
        self,
        receiver_id: UUID,
        lease_token: UUID,
        state: ReceiverState,
        *,
        error: Any | None = None,
    ) -> DoccleReceiver | None:
        if state is ReceiverState.PROVISIONING:
            raise ValueError("Provisioning completion requires a terminal state")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE doccle_receivers
                SET state = $3, error = $4::jsonb,
                    provisioning_lease_token = NULL,
                    provisioning_lease_until = NULL,
                    updated_at = now()
                WHERE id = $1 AND state = 'provisioning'
                  AND provisioning_lease_token = $2
                  AND provisioning_lease_until > now()
                RETURNING *
                """,
                receiver_id,
                lease_token,
                state.value,
                _json(error) if error is not None else None,
            )
        return _receiver(row) if row is not None else None

    async def compare_and_set_state(
        self,
        receiver_id: UUID,
        expected: ReceiverState | str | Iterable[ReceiverState | str],
        state: ReceiverState | str,
        *,
        linked: bool | None = None,
        error: Any | None = None,
    ) -> DoccleReceiver | None:
        if ReceiverState(state) is ReceiverState.PROVISIONING:
            raise ValueError(
                "Use claim_provisioning to establish durable Receiver ownership"
            )
        expected_states = _states(expected)
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE doccle_receivers SET state = $3,
                    linked = COALESCE($4, linked), error = $5::jsonb,
                    updated_at = now()
                 WHERE id = $1 AND state = ANY($2::doccle_receiver_state[])
                RETURNING *
                """,
                receiver_id,
                expected_states,
                str(state),
                linked,
                _json(error) if error is not None else None,
            )
        return _receiver(row) if row is not None else None

    transition = compare_and_set_state

    async def apply_callback(
        self,
        *,
        destination: str,
        callback_identity: str,
        receiver_id: UUID,
        external_receiver_id: str,
        linked: bool,
        payload: Any,
    ) -> CallbackResult:
        if not callback_identity:
            raise ValueError("Doccle callback identity must not be empty")
        callback_id = uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchrow(
                """
                INSERT INTO doccle_receiver_callbacks (
                    id, destination, callback_identity, receiver_id,
                    external_receiver_id, linked, payload
                ) VALUES (
                    $1, $2, $3,
                    (SELECT _id FROM doccle_receivers WHERE id = $4),
                    $5, $6, $7::jsonb
                )
                RETURNING *
                """,
                callback_id,
                destination,
                callback_identity,
                receiver_id,
                external_receiver_id,
                linked,
                _json(payload),
            )
            receiver_row = await connection.fetchrow(
                """
                UPDATE doccle_receivers
                SET linked = $3, linked_receipt_order = $5, updated_at = now()
                WHERE id = $1 AND destination = $2 AND external_receiver_id = $4
                  AND (linked_receipt_order IS NULL OR linked_receipt_order < $5)
                RETURNING *
                """,
                receiver_id,
                destination,
                linked,
                external_receiver_id,
                inserted["receipt_order"],
            )
            if receiver_row is None:
                receiver_row = await connection.fetchrow(
                    "SELECT * FROM doccle_receivers "
                    "WHERE id = $1 AND destination = $2 AND external_receiver_id = $3",
                    receiver_id,
                    destination,
                    external_receiver_id,
                )
                if receiver_row is None:
                    raise LookupError("Doccle callback receiver does not exist")
            callback_row = await connection.fetchrow(
                "UPDATE doccle_receiver_callbacks SET applied = $2 "
                "WHERE id = $1 "
                "RETURNING id, destination, callback_identity, "
                "$3::uuid AS receiver_id, external_receiver_id, linked, payload, "
                "applied, received_at, receipt_order",
                callback_id,
                receiver_row["linked_receipt_order"] == inserted["receipt_order"],
                receiver_id,
            )
        return CallbackResult(_receiver(receiver_row), _callback(callback_row), False)


def _validate_subject(subject: dict[str, Any]) -> None:
    if not isinstance(subject, dict) or not subject:
        raise ValueError("Doccle receiver subject must be a nonempty object")


def _states(value: Any) -> list[str]:
    return [str(value)] if isinstance(value, str) else [str(item) for item in value]
