from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile

from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from barcode.writer import BaseWriter
from postal_station.models import DocumentBoundary
from postal_station.models import PrintSides as StationPrintSides
from postal_station.rendering import traveller as postal_traveller
from postal_station.rendering.composition import compose_letter_pdf
from postal_station.rendering.composition import merge_pdf_documents
from postal_station.rendering.traveller import PdfBarcodeWriter
from postal_station.rendering.traveller import _single_page_pdf
from postal_station.rendering.traveller import render_code128
from postal_station.rendering.traveller import render_traveller

from xarta.adapters import AdapterRegistry
from xarta.execution import ExecutionMode
from xarta.services.v1.postal.adapters import build_postal_components
from xarta.services.v1.postal.adapters.local.auth import StationRegistry
from xarta.services.v1.postal.adapters.local.models import PrintColorMode
from xarta.services.v1.postal.adapters.local.models import PrintSides
from xarta.services.v1.postal.adapters.local.reducers import reduce_station_scan
from xarta.services.v1.postal.adapters.local.repository import LocalPostalRepository
from xarta.services.v1.postal.adapters.local.repository import RunLimits
from xarta.services.v1.postal.adapters.local.repository import _jsonb_object
from xarta.services.v1.postal.adapters.local.service import LocalPostalService
from xarta.services.v1.postal.adapters.local.workflow import PhysicalState
from xarta.services.v1.postal.adapters.local.workflow import PrintJobKind
from xarta.services.v1.postal.adapters.local.workflow import ScanMode


class _Adapter:
    def __init__(self, mode: ExecutionMode) -> None:
        self.execution_mode = mode


class _Factory:
    def validate(self, configuration) -> None:
        pass

    def create(self, configuration):
        return _Adapter(ExecutionMode(configuration["execution_mode"]))


class _AsyncContext:
    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return None


def test_postal_registry_validates_retained_revision_mode_invariance() -> None:
    configurations = {
        "mailroom": {
            "kind": "postal",
            "adapter": "fake",
            "current_revision": "2",
            "revisions": {
                "1": {"execution_mode": "tracked"},
                "2": {"execution_mode": "synchronous"},
            },
        }
    }
    with pytest.raises(ValueError, match="changes execution mode"):
        build_postal_components(configurations, AdapterRegistry({"fake": _Factory()}))


def test_station_registry_enforces_bearer_identity_and_site_scope() -> None:
    registry = StationRegistry(
        {
            "station-1": {"site": "brussels", "enabled": True, "api_token": "secret"},
            "disabled": {"site": "antwerp", "enabled": False},
        }
    )
    identity = registry.authenticate("Bearer secret")
    assert (identity.id, identity.site) == ("station-1", "brussels")
    registry.require_station(identity, "station-1")
    with pytest.raises(PermissionError):
        registry.require_station(identity, "disabled")
    with pytest.raises(PermissionError):
        registry.authenticate("Bearer wrong")


def test_postgresql_jsonb_object_accepts_default_asyncpg_text_and_decoded_values() -> (
    None
):
    assert _jsonb_object('{"print":{"sides":"simplex"}}') == {
        "print": {"sides": "simplex"}
    }
    assert _jsonb_object({"print": {"sides": "duplex_long_edge"}}) == {
        "print": {"sides": "duplex_long_edge"}
    }
    with pytest.raises(ValueError, match="must be an object"):
        _jsonb_object("[]")


async def test_releasing_unpublished_run_removes_items_before_reclaim() -> None:
    statements = []

    class Connection:
        def transaction(self):
            return _AsyncContext()

        async def fetchrow(self, query, *arguments):
            return {"_id": 42, "package_checksum": None}

        async def execute(self, query, *arguments):
            statements.append(" ".join(query.split()))

    class Pool:
        def acquire(self):
            return _AsyncContext(Connection())

    repository = LocalPostalRepository(Pool())

    await repository.release_unpublished_run(uuid4())

    assert statements == [
        "UPDATE postal_local.production_tasks task SET assignment_state = 'pending', updated_at = now() FROM postal_local.production_run_items item WHERE item.production_run_id = $1 AND item.production_task_id = task._id AND task.assignment_state = 'claimed'",
        "DELETE FROM postal_local.production_run_items WHERE production_run_id = $1",
        "UPDATE postal_local.production_runs SET state = 'cancelled', updated_at = now() WHERE _id = $1",
    ]


async def test_empty_claim_is_not_left_as_an_active_run() -> None:
    statements = []

    class Connection:
        def transaction(self):
            return _AsyncContext()

        async def fetchrow(self, query, *arguments):
            return None

        async def fetchval(self, query, *arguments):
            return 42

        async def fetch(self, query, *arguments):
            return []

        async def execute(self, query, *arguments):
            statements.append(" ".join(query.split()))

    class Pool:
        def acquire(self):
            return _AsyncContext(Connection())

    run = await LocalPostalRepository(Pool()).claim_run(
        station_id="station-1",
        site_id="brussels",
        idempotency_key="idle-claim",
        claim_token="token",
        limits=RunLimits(),
    )

    assert run.task_ids == ()
    assert statements == [
        "UPDATE postal_local.production_runs SET state = 'cancelled', updated_at = now() WHERE _id = $1"
    ]


async def test_handover_batch_idempotency_replays_the_original_batch() -> None:
    task_id = uuid4()
    batch_id = uuid4()
    fetchrows = [
        None,
        {
            "id": batch_id,
            "site_id": "brussels",
            "service_date": date(2026, 8, 31),
            "operator_reference": "mock-station:station-1",
            "task_ids": [task_id],
        },
    ]

    class Connection:
        def transaction(self):
            return _AsyncContext()

        async def fetchrow(self, query, *arguments):
            return fetchrows.pop(0)

    class Pool:
        def acquire(self):
            return _AsyncContext(Connection())

    result = await LocalPostalRepository(Pool()).create_handover_batch(
        station_id="station-1",
        site_id="brussels",
        service_date=date(2026, 8, 31),
        task_ids=(task_id,),
        operator_reference="mock-station:station-1",
        idempotency_key="batch-1",
    )

    assert result == (batch_id, True)


async def test_confirm_handover_completes_station_task_batch_and_run() -> None:
    operation_id = uuid4()
    task_id = uuid4()
    batch_id = uuid4()
    statements = []
    fetchvals = [7, None]

    class Connection:
        async def fetchrow(self, query, *arguments):
            return {
                "_id": 11,
                "generation": 1,
                "physical_state": "ready_for_handover",
                "operation_id": 12,
            }

        async def fetchval(self, query, *arguments):
            return fetchvals.pop(0)

        async def execute(self, query, *arguments):
            statements.append(" ".join(query.split()))

    target = SimpleNamespace(
        operation_id=operation_id,
        adapter="local",
        task_id=task_id,
        generation=1,
        service="ordinary",
        physical_state=PhysicalState.READY_FOR_HANDOVER,
        handover_batch_id=batch_id,
    )

    await object.__new__(LocalPostalRepository).apply_scan(
        Connection(),
        SimpleNamespace(external_event_id="confirm-handover"),
        target=target,
        mode=ScanMode.CONFIRM_HANDOVER,
        station_id="station-1",
        occurred_at=datetime.now(UTC),
        handover_batch_id=batch_id,
    )

    assert any(
        "assignment_state = 'completed'" in statement for statement in statements
    )
    assert any(
        "UPDATE postal_local.handover_batches" in statement
        and "state = 'completed'" in statement
        for statement in statements
    )
    assert any(
        "UPDATE postal_local.production_runs" in statement
        and "state = 'completed'" in statement
        for statement in statements
    )


def test_pdf_composition_preserves_order_and_inserts_recto_alignment_blank() -> None:
    first = _single_page_pdf("first")
    second = _single_page_pdf("second")

    merged = merge_pdf_documents(
        (first, second),
        sides=StationPrintSides.DUPLEX_LONG_EDGE,
        boundary=DocumentBoundary.START_ON_RECTO,
    )

    from pyhanko.pdf_utils.reader import PdfFileReader

    reader = PdfFileReader(io.BytesIO(merged), strict=True)
    assert int(reader.root["/Pages"]["/Count"]) == 3


def test_simplex_letter_composition_places_content_after_traveller() -> None:
    letter = compose_letter_pdf(
        _single_page_pdf("traveller"),
        _single_page_pdf("content"),
        sides=StationPrintSides.SIMPLEX,
    )

    streams = _page_stream_bytes(letter)
    assert len(streams) == 2
    assert b"traveller" in streams[0]
    assert b"content" in streams[1]


def test_duplex_letter_composition_inserts_blank_page_two() -> None:
    letter = compose_letter_pdf(
        _single_page_pdf("traveller"),
        _single_page_pdf("content"),
        sides=StationPrintSides.DUPLEX_SHORT_EDGE,
    )

    streams = _page_stream_bytes(letter)
    assert len(streams) == 3
    assert b"traveller" in streams[0]
    assert streams[1].strip() == b""
    assert b"content" in streams[2]


def test_traveller_renders_code128_and_keeps_uuid_off_detachable_label() -> None:
    task_id = uuid4()
    item = SimpleNamespace(
        task_id=task_id,
        sequence=1,
        operation_id=uuid4(),
        plan_digest="d" * 64,
        sources=((1, "source", "document"),),
        plan={
            "mailpiece": {
                "service": {"type": "ordinary", "speed": "priority"},
                "recipient": {
                    "name": "Recipient",
                    "address": {
                        "street": "Street",
                        "house_number": "1",
                        "postal_code": "1000",
                        "city": "Brussels",
                    },
                },
            },
            "print": {"color_mode": "monochrome", "sides": "simplex"},
            "quantities": {"estimated_weight_g": "14.98"},
            "franking": {
                "provider": {"id": "bpost", "profile": {"id": "dev", "revision": "1"}},
                "method": "stamps",
                "policy_revision": "1",
                "supplies": [
                    {"reference": "unit", "description": "Unit stamp", "quantity": 1}
                ],
                "pricing": {
                    "tariff_revision": "1",
                    "currency": "EUR",
                    "lines": [
                        {
                            "reference": "unit",
                            "quantity": 1,
                            "unit_price": "1.50",
                            "amount": "1.50",
                        }
                    ],
                    "total_postage": "1.50",
                },
            },
        },
    )

    instructions = SimpleNamespace(
        service=item.plan["mailpiece"]["service"],
        recipient=item.plan["mailpiece"]["recipient"],
        quantities=item.plan["quantities"],
        franking=item.plan["franking"],
    )
    stream = _page_stream_bytes(
        render_traveller(str(item.task_id), len(item.sources), instructions)
    )[0]
    marker = stream.index(b"/XartaLabel BMC")

    assert str(task_id).encode() in stream[:marker]
    assert str(task_id).encode() not in stream[marker:]
    assert b"postal:" not in stream
    assert b"/XartaBarcode BMC" in stream[:marker]
    assert stream[:marker].count(b" re f") > 50
    assert b" re f" not in stream[marker:]


def test_code128_uses_python_barcode_base_writer_and_pdf_rectangles(
    monkeypatch,
) -> None:
    payload = str(uuid4())
    observed = {}
    barcode_get = postal_traveller.barcode.get

    def record_get(name, code, writer):
        observed.update(name=name, code=code, writer=writer)
        return barcode_get(name, code, writer=writer)

    monkeypatch.setattr(postal_traveller.barcode, "get", record_get)

    rendered = render_code128(payload)

    assert observed["name"] == "code128"
    assert observed["code"] == payload
    assert isinstance(observed["writer"], PdfBarcodeWriter)
    assert isinstance(observed["writer"], BaseWriter)
    assert rendered.commands.count(b" re f") > 50
    first_bar_x = float(
        next(
            line for line in rendered.commands.splitlines() if line.endswith(b" re f")
        ).split()[0]
    )
    assert first_bar_x >= 4 * 72 / 25.4


async def test_repository_rejects_prefixed_scan_uuid() -> None:
    repository = object.__new__(LocalPostalRepository)

    with pytest.raises(ValueError, match="Invalid postal traveller barcode"):
        await repository.scan_target(f"postal:{uuid4()}", "station-1", "brussels")


def _page_stream_bytes(document: bytes) -> list[bytes]:
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.reader import PdfFileReader

    reader = PdfFileReader(io.BytesIO(document), strict=True)
    result = []
    for page_index in range(int(reader.root["/Pages"]["/Count"])):
        page_ref, _ = reader.find_page_for_modification(page_index)
        contents = page_ref.get_object().get("/Contents")
        if contents is None:
            result.append(b"")
            continue
        contents = contents.get_object()
        streams = contents if isinstance(contents, generic.ArrayObject) else (contents,)
        result.append(b"\n".join(stream.get_object().data for stream in streams))
    return result


async def test_scan_feedback_builds_repository_mutation_on_tracking_connection() -> (
    None
):
    operation_id = uuid4()
    flow_id = uuid4()
    correlation_id = uuid4()
    node_execution_id = uuid4()
    task_id = uuid4()
    connection = object()
    context = object()

    class Repository:
        mutation = None

        async def scan_target(self, barcode, station_id, site_id):
            return SimpleNamespace(
                operation_id=operation_id,
                adapter="local",
                flow_id=flow_id,
                correlation_id=correlation_id,
                node_execution_id=node_execution_id,
                task_id=task_id,
                generation=1,
                service="ordinary",
                physical_state=PhysicalState.PROCESSING,
                handover_batch_id=None,
            )

        async def apply_scan(self, supplied_connection, supplied_context, **values):
            self.mutation = supplied_connection, supplied_context, values

    class Tracking:
        async def feedback(self, *args, transaction_mutation, **kwargs):
            await transaction_mutation(connection, context)
            return SimpleNamespace(duplicate=False)

    class Log:
        def __init__(self):
            self.events = []

        async def ainfo(self, event, **fields):
            self.events.append((event, fields))

    repository = Repository()
    log = Log()
    service = LocalPostalService(repository, Tracking(), log)  # type: ignore[arg-type]
    target, _ = await service.scan(
        station_id="station-1",
        site_id="brussels",
        barcode=str(task_id),
        mode=ScanMode.READY_FOR_HANDOVER,
        occurred_at=datetime.now(UTC),
    )

    assert target.task_id == task_id
    assert repository.mutation is not None
    assert repository.mutation[:2] == (connection, context)
    assert log.events == [
        (
            "postal_physical_state_changed",
            {
                "flow_id": str(flow_id),
                "correlation_id": str(correlation_id),
                "node_execution_id": str(node_execution_id),
                "operation_id": str(operation_id),
                "adapter": "local",
                "production_task_id": str(task_id),
                "generation": 1,
                "scan_mode": "ready_for_handover",
                "previous_state": "processing",
                "next_state": "ready_for_handover",
                "handover_batch_id": None,
            },
        )
    ]


def test_handover_reducer_resolves_only_ordinary_mail() -> None:
    ordinary = reduce_station_scan({}, (ScanMode.CONFIRM_HANDOVER, "ordinary"))
    registered = reduce_station_scan({}, (ScanMode.CONFIRM_HANDOVER, "registered"))

    assert ordinary.operation_resolved
    assert not registered.operation_resolved
    assert [outcome.outcome for outcome in ordinary.outcomes] == ["handed_over"]
