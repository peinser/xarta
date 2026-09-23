from __future__ import annotations

import asyncio
import datetime

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest

from xarta.services.v1.doccle.adapters import DoccleAmbiguousTransportError
from xarta.services.v1.doccle.adapters import DoccleSafePreTransmissionError
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResult
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import ReceiverState
from xarta.services.v1.doccle.service import ReceiverService

NOW = datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC)


class ReceiverRepository:
    def __init__(self) -> None:
        self.receiver: DoccleReceiver | None = None
        self.lock = asyncio.Lock()

    async def insert_or_load(self, destination, subject):
        async with self.lock:
            if self.receiver is None:
                identifier = uuid4()
                self.receiver = DoccleReceiver(
                    identifier,
                    destination,
                    subject,
                    str(identifier),
                    ReceiverState.PENDING,
                    None,
                    None,
                    NOW,
                    NOW,
                )
            return self.receiver

    async def load_by_subject(self, destination, subject):
        if self.receiver and (self.receiver.destination, self.receiver.subject) == (
            destination,
            subject,
        ):
            return self.receiver
        return None

    async def load(self, receiver_id: UUID):
        return (
            self.receiver if self.receiver and self.receiver.id == receiver_id else None
        )

    async def claim_provisioning(self, receiver_id: UUID, *, lease_seconds: float):
        async with self.lock:
            if self.receiver is None or self.receiver.id != receiver_id:
                return None
            lease_expired = (
                self.receiver.state is ReceiverState.PROVISIONING
                and self.receiver.provisioning_lease_until is not None
                and self.receiver.provisioning_lease_until <= NOW
            )
            if (
                self.receiver.state
                not in {
                    ReceiverState.PENDING,
                    ReceiverState.FAILED,
                    ReceiverState.UNCERTAIN,
                }
                and not lease_expired
            ):
                return None
            self.receiver = replace(
                self.receiver,
                state=ReceiverState.PROVISIONING,
                provisioning_lease_token=uuid4(),
                provisioning_lease_until=NOW
                + datetime.timedelta(seconds=lease_seconds),
            )
            return self.receiver

    async def complete_provisioning(self, receiver_id, lease_token, state, **changes):
        async with self.lock:
            if (
                self.receiver is None
                or self.receiver.id != receiver_id
                or self.receiver.provisioning_lease_token != lease_token
            ):
                return None
            self.receiver = replace(
                self.receiver,
                state=state,
                provisioning_lease_token=None,
                provisioning_lease_until=None,
                **{key: value for key, value in changes.items() if value is not None},
            )
            return self.receiver

    async def compare_and_set_state(self, receiver_id, expected, state, **changes):
        async with self.lock:
            if (
                self.receiver is None
                or self.receiver.id != receiver_id
                or self.receiver.state is not expected
            ):
                return None
            self.receiver = replace(
                self.receiver,
                state=state,
                **{key: value for key, value in changes.items() if value is not None},
            )
            return self.receiver


class Adapter:
    def __init__(self, results) -> None:
        self.results = iter(results)
        self.receiver_ids: list[str] = []

    async def create_or_update_receiver(self, *, receiver_id, profile):
        assert profile == DoccleReceiverProfile(label="Customer")
        self.receiver_ids.append(receiver_id)
        await asyncio.sleep(0)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def service(results):
    repository = ReceiverRepository()
    adapter = Adapter(results)
    binding = SimpleNamespace(destination="primary", adapter="fake")
    resolved = SimpleNamespace(binding=binding, configuration={})
    components = SimpleNamespace(
        sender="primary",
        resolve_sender=lambda: resolved,
        adapters=SimpleNamespace(create=lambda *_args: adapter),
    )
    return ReceiverService(repository, components), repository, adapter  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_first_repeated_and_concurrent_ensure_keep_one_stable_identity() -> None:
    receiver_service, repository, adapter = service(
        [DoccleResult(DoccleResultCategory.PROVISIONED)]
    )
    subject = {"customer": "42"}
    profile = DoccleReceiverProfile(label="Customer")

    first, concurrent = await asyncio.gather(
        receiver_service.ensure(subject, profile),
        receiver_service.ensure(subject, profile),
    )
    repeated = await receiver_service.ensure(subject, profile)

    assert first.id == concurrent.id == repeated.id
    assert first.external_receiver_id == concurrent.external_receiver_id
    assert repository.receiver is not None
    assert repository.receiver.state is ReceiverState.PROVISIONED
    assert adapter.receiver_ids == [first.external_receiver_id]
    assert await receiver_service.resolve(subject) == repository.receiver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_result", "failed_state"),
    [
        (DoccleSafePreTransmissionError("not sent"), ReceiverState.FAILED),
        (DoccleAmbiguousTransportError("maybe sent"), ReceiverState.UNCERTAIN),
        (
            DoccleResult(DoccleResultCategory.BUSINESS_REJECTION, provider_status="NO"),
            ReceiverState.FAILED,
        ),
    ],
)
async def test_failed_or_uncertain_receiver_retry_reuses_provider_identity(
    first_result, failed_state
) -> None:
    receiver_service, _, adapter = service(
        [first_result, DoccleResult(DoccleResultCategory.PROVISIONED)]
    )
    profile = DoccleReceiverProfile(label="Customer")

    failed = await receiver_service.ensure({"customer": "42"}, profile)
    provisioned = await receiver_service.ensure({"customer": "42"}, profile)

    assert failed.state is failed_state
    assert provisioned.state is ReceiverState.PROVISIONED
    assert failed.id == provisioned.id
    assert adapter.receiver_ids == [failed.external_receiver_id] * 2


@pytest.mark.asyncio
async def test_expired_provisioning_is_reclaimed_with_same_receiver_identity() -> None:
    receiver_service, repository, adapter = service(
        [DoccleResult(DoccleResultCategory.PROVISIONED)]
    )
    subject = {"customer": "42"}
    receiver = await repository.insert_or_load("primary", subject)
    abandoned = await repository.claim_provisioning(receiver.id, lease_seconds=30)
    assert abandoned is not None and abandoned.provisioning_lease_token is not None
    repository.receiver = replace(
        abandoned,
        provisioning_lease_until=NOW - datetime.timedelta(seconds=1),
    )

    recovered = await receiver_service.ensure(
        subject, DoccleReceiverProfile(label="Customer")
    )
    stale_completion = await repository.complete_provisioning(
        receiver.id,
        abandoned.provisioning_lease_token,
        ReceiverState.FAILED,
    )

    assert recovered.state is ReceiverState.PROVISIONED
    assert recovered.external_receiver_id == receiver.external_receiver_id
    assert adapter.receiver_ids == [receiver.external_receiver_id]
    assert stale_completion is None
