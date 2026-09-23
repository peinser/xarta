from __future__ import annotations

import pytest

from package_factory import RUN_ID
from postal_station import cli
from postal_station.cli import main
from postal_station.cli import run_mock
from postal_station.cli import run_once
from postal_station.cli import station_config
from postal_station.configuration import StationConfig
from postal_station.mock import MockStationResult
from postal_station.models import ClaimedRun
from postal_station.printing.fake import FakePrinter


def test_fake_station_does_not_require_a_printer(tmp_path):
    config = station_config(
        cups=False,
        env={
            "POSTAL_STATION_API_URL": "http://127.0.0.1:8000",
            "POSTAL_STATION_ID": "development-station",
            "POSTAL_STATION_CACHE_DIR": str(tmp_path),
        },
    )

    assert config.printer == "fake"


def test_cups_station_requires_explicit_confirmation(monkeypatch):
    monkeypatch.delenv("CONFIRM_PHYSICAL_PRINT", raising=False)

    with pytest.raises(SystemExit) as raised:
        main(["--cups"])

    assert raised.value.code == 2


def test_mock_and_cups_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit) as raised:
        main(["--mock", "--cups"])

    assert raised.value.code == 2


async def test_mock_mode_can_only_construct_the_fake_printer(monkeypatch, tmp_path):
    observed = None

    class Runner:
        def __init__(self, config, api, station, poll_interval_seconds):
            nonlocal observed
            observed = station.printer

        async def run(self, stop):
            return MockStationResult(2, 3)

    monkeypatch.setattr(cli, "MockPostalStation", Runner)
    monkeypatch.setattr(
        cli,
        "CupsPrinter",
        lambda queue: (_ for _ in ()).throw(
            AssertionError("mock mode must not construct CUPS")
        ),
    )
    config = StationConfig(
        "http://127.0.0.1:8000", "development-station", "fake", tmp_path
    )

    result = await run_mock(
        config, poll_interval_seconds=0.01, stop=cli.asyncio.Event()
    )

    assert isinstance(observed, FakePrinter)
    assert result == {
        "status": "stopped",
        "completed_runs": 2,
        "completed_letters": 3,
        "printer": "fake",
    }


@pytest.mark.parametrize(("cups", "limits"), [(False, None), (True, {"items": 1})])
async def test_station_claims_once_and_exits_when_idle(
    monkeypatch, tmp_path, cups, limits
):
    class Api:
        async def claim_run(self, idempotency_key, received_limits=None):
            assert idempotency_key == "claim-1"
            assert received_limits == limits
            return ClaimedRun(RUN_ID, "token", (), False)

    api = Api()
    monkeypatch.setattr(cli, "UrllibPostalStationApi", lambda config: api)
    config = StationConfig(
        "http://127.0.0.1:8000", "development-station", "queue", tmp_path
    )

    result = await run_once(config, cups=cups, idempotency_key="claim-1")

    assert result == {"status": "idle", "run_id": RUN_ID, "claimed_tasks": 0}
