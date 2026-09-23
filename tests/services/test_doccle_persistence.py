from __future__ import annotations

import asyncio
import os

from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from xarta.services.v1.doccle import ReceiverRepository
from xarta.services.v1.doccle import ReceiverState

pytestmark = pytest.mark.skipif(
    "TRACKING_POSTGRES_TEST_DSN" not in os.environ,
    reason="tracking PostgreSQL integration database is not configured",
)


@pytest_asyncio.fixture
async def repositories():
    pool = await asyncpg.create_pool(os.environ["TRACKING_POSTGRES_TEST_DSN"])
    async with pool.acquire() as connection:
        await connection.execute(
            "TRUNCATE doccle_receiver_callbacks, doccle_receivers CASCADE"
        )
    yield ReceiverRepository(pool), None, pool
    await pool.close()


@pytest.mark.asyncio
async def test_receiver_subject_uses_exact_jsonb_object_equality(repositories) -> None:
    receivers, _, _ = repositories
    receiver = await receivers.insert_or_load(
        "primary", {"enterprise": "42", "person": {"id": 7}}
    )

    reordered = await receivers.load_by_subject(
        "primary", {"person": {"id": 7}, "enterprise": "42"}
    )
    superset = await receivers.load_by_subject(
        "primary", {"enterprise": "42", "person": {"id": 7}, "extra": True}
    )
    other_destination = await receivers.load_by_subject(
        "secondary", {"enterprise": "42", "person": {"id": 7}}
    )

    assert reordered == receiver
    assert superset is None
    assert other_destination is None
    with pytest.raises(ValueError):
        await receivers.insert_or_load("primary", {})


@pytest.mark.asyncio
async def test_all_subject_identity_edges_use_postgresql_jsonb_equality(
    repositories,
) -> None:
    receivers, _, pool = repositories
    subject = {
        "customer": "Value ",
        "nullable": None,
        "ordered": ["first", "second"],
    }
    stored = await receivers.insert_or_load("primary", subject)

    assert (
        await receivers.load_by_subject(
            "primary",
            {"ordered": ["first", "second"], "nullable": None, "customer": "Value "},
        )
        == stored
    )
    assert await receivers.load_by_subject("primary", {"customer": "Value "}) is None
    assert (
        await receivers.load_by_subject("primary", {**subject, "extra": "attribute"})
        is None
    )
    assert (
        await receivers.load_by_subject(
            "primary", {"customer": "Value ", "ordered": ["first", "second"]}
        )
        is None
    )
    assert (
        await receivers.load_by_subject("primary", {**subject, "customer": "value "})
        is None
    )
    assert (
        await receivers.load_by_subject("primary", {**subject, "customer": "Value"})
        is None
    )
    assert (
        await receivers.load_by_subject(
            "primary", {**subject, "ordered": ["second", "first"]}
        )
        is None
    )

    same_other_destination = await receivers.insert_or_load("secondary", subject)
    assert same_other_destination.id != stored.id
    assert await receivers.insert_or_load("primary", dict(subject)) == stored

    async with pool.acquire() as connection:
        # PostgreSQL parses formatting differences before applying jsonb equality.
        assert (
            await connection.fetchval(
                "SELECT id FROM doccle_receivers "
                "WHERE destination = $1 AND subject = $2::jsonb",
                "primary",
                '{ "ordered" : ["first", "second"], "customer" : "Value ", '
                '"nullable" : null }',
            )
            == stored.id
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO doccle_receivers "
                "(id, destination, subject, external_receiver_id, state) "
                "VALUES ($1, 'primary', $2::jsonb, $3, 'pending')",
                uuid4(),
                "[]",
                str(uuid4()),
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO doccle_receivers "
                "(id, destination, subject, external_receiver_id, state) "
                "VALUES ($1, 'primary', '{}'::jsonb, $2, 'pending')",
                uuid4(),
                str(uuid4()),
            )


@pytest.mark.asyncio
async def test_receiver_insert_or_load_is_concurrency_safe(repositories) -> None:
    receivers, _, _ = repositories
    created = await asyncio.gather(
        *(receivers.insert_or_load("primary", {"number": "123"}) for _ in range(12))
    )

    assert len({receiver.id for receiver in created}) == 1
    assert {receiver.state for receiver in created} == {ReceiverState.PENDING}
    assert len({receiver.external_receiver_id for receiver in created}) == 1
    assert created[0].external_receiver_id


@pytest.mark.asyncio
async def test_receiver_cas_preserves_inserted_external_identity(repositories) -> None:
    receivers, _, pool = repositories
    first = await receivers.insert_or_load("primary", {"number": "1"})
    second = await receivers.insert_or_load("primary", {"number": "2"})

    provisioning = await receivers.claim_provisioning(first.id, lease_seconds=30)
    assert provisioning is not None
    assert provisioning.provisioning_lease_token is not None
    stale = await receivers.claim_provisioning(first.id, lease_seconds=30)
    provisioned = await receivers.complete_provisioning(
        first.id,
        provisioning.provisioning_lease_token,
        ReceiverState.PROVISIONED,
    )

    assert provisioning is not None
    assert stale is None
    assert provisioned is not None
    assert provisioned.external_receiver_id == first.external_receiver_id
    assert second.external_receiver_id != first.external_receiver_id


@pytest.mark.asyncio
async def test_expired_receiver_claim_is_reclaimed_without_changing_identity(
    repositories,
) -> None:
    receivers, _, pool = repositories
    receiver = await receivers.insert_or_load("primary", {"number": "1"})
    first = await receivers.claim_provisioning(receiver.id, lease_seconds=30)
    assert first is not None and first.provisioning_lease_token is not None
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE doccle_receivers SET provisioning_lease_until = now() - interval '1 second' WHERE id = $1",
            receiver.id,
        )
    reclaimed = await receivers.claim_provisioning(receiver.id, lease_seconds=30)
    assert reclaimed is not None and reclaimed.provisioning_lease_token is not None

    stale = await receivers.complete_provisioning(
        receiver.id, first.provisioning_lease_token, ReceiverState.FAILED
    )
    completed = await receivers.complete_provisioning(
        receiver.id,
        reclaimed.provisioning_lease_token,
        ReceiverState.PROVISIONED,
    )

    assert stale is None
    assert completed is not None
    assert completed.external_receiver_id == receiver.external_receiver_id


@pytest.mark.asyncio
async def test_callback_receipts_are_state_projection_not_payload_deduplication(
    repositories,
) -> None:
    receivers, _, pool = repositories
    receiver = await receivers.insert_or_load("primary", {"number": "1"})
    external_id = receiver.external_receiver_id

    transitions = [True, False, True]
    results = []
    for linked in transitions:
        results.append(
            await receivers.apply_callback(
                destination="primary",
                callback_identity=f"same-payload-{linked}",
                receiver_id=receiver.id,
                external_receiver_id=external_id,
                linked=linked,
                payload={"linked": linked},
            )
        )

    assert results[-1].receiver.linked is True
    assert [result.callback.applied for result in results] == [True, True, True]
    assert [result.callback.receipt_order for result in results] == sorted(
        result.callback.receipt_order for result in results
    )
    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM doccle_receiver_callbacks"
        ) == len(transitions)


@pytest.mark.asyncio
async def test_concurrent_identical_callback_receipts_are_both_harmless(
    repositories,
) -> None:
    receivers, _, pool = repositories
    receiver = await receivers.insert_or_load("primary", {"number": "1"})
    external_id = receiver.external_receiver_id

    first, replay = await asyncio.gather(
        receivers.apply_callback(
            destination="primary",
            callback_identity="callback-1",
            receiver_id=receiver.id,
            external_receiver_id=external_id,
            linked=True,
            payload={"event": "linked", "sequence": 1},
        ),
        receivers.apply_callback(
            destination="primary",
            callback_identity="callback-1",
            receiver_id=receiver.id,
            external_receiver_id=external_id,
            linked=True,
            payload={"event": "linked", "sequence": 1},
        ),
    )

    assert first.duplicate is False
    assert replay.duplicate is False
    assert first.callback.id != replay.callback.id
    assert first.receiver.linked is True
    assert replay.receiver.linked is True
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM doccle_receiver_callbacks")
            == 2
        )
