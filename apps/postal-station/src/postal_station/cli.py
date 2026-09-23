"""Command-line entry point for one-shot and mock postal stations."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal

from collections.abc import Mapping
from collections.abc import Sequence
from uuid import uuid4

from postal_station.api_client import UrllibPostalStationApi
from postal_station.application import PostalStation
from postal_station.configuration import StationConfig
from postal_station.mock import MockPostalStation
from postal_station.packages import PackageVerifier
from postal_station.printing.cups import CupsPrinter
from postal_station.printing.fake import FakePrinter


def station_config(
    *, cups: bool, env: Mapping[str, str] | None = None
) -> StationConfig:
    values = dict(os.environ if env is None else env)
    if not cups:
        values.setdefault("POSTAL_STATION_PRINTER", "fake")
    return StationConfig.from_env(values)


async def run_once(
    config: StationConfig, *, cups: bool, idempotency_key: str
) -> dict[str, object]:
    api = UrllibPostalStationApi(config)
    printer = CupsPrinter(config.printer) if cups else FakePrinter()
    claimed = await api.claim_run(idempotency_key, {"items": 1} if cups else None)
    if not claimed.task_ids:
        return {"status": "idle", "run_id": claimed.id, "claimed_tasks": 0}

    station = PostalStation(config, api, printer, PackageVerifier())
    await station.process_run(claimed.id)
    result: dict[str, object] = {
        "status": "completed",
        "run_id": claimed.id,
        "claimed_tasks": len(claimed.task_ids),
        "printer": config.printer if cups else "fake",
    }
    if isinstance(printer, FakePrinter):
        result["rendered_files"] = [
            str(submission.document) for submission in printer.submissions
        ]
    return result


async def run_mock(
    config: StationConfig,
    *,
    poll_interval_seconds: float,
    stop: asyncio.Event | None = None,
) -> dict[str, object]:
    if poll_interval_seconds <= 0:
        raise ValueError("poll interval must be greater than zero")
    api = UrllibPostalStationApi(config)
    printer = FakePrinter()
    station = PostalStation(config, api, printer, PackageVerifier())
    mock = MockPostalStation(config, api, station, poll_interval_seconds)
    stop_event = stop or asyncio.Event()
    if stop is None:
        loop = asyncio.get_running_loop()
        for name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(name, stop_event.set)
    result = await mock.run(stop_event)
    return {
        "status": "stopped",
        "completed_runs": result.completed_runs,
        "completed_letters": result.completed_letters,
        "printer": "fake",
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run a Xarta postal production station."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cups",
        action="store_true",
        help="submit to POSTAL_STATION_PRINTER instead of the safe fake printer",
    )
    mode.add_argument(
        "--mock",
        action="store_true",
        help="continuously fake-print and simulate scans through handover",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="claim idempotency key; defaults to a new key for this invocation",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=1.0,
        help="idle and transient-error delay for --mock (default: 1)",
    )
    arguments = parser.parse_args(argv)
    if arguments.cups and os.environ.get("CONFIRM_PHYSICAL_PRINT") != "yes":
        parser.error("--cups requires CONFIRM_PHYSICAL_PRINT=yes")

    try:
        config = station_config(cups=arguments.cups)
        if arguments.mock:
            if arguments.idempotency_key is not None:
                parser.error("--idempotency-key cannot be used with --mock")
            result = asyncio.run(
                run_mock(config, poll_interval_seconds=arguments.poll_interval_seconds)
            )
        else:
            key = arguments.idempotency_key or (
                f"station-cli:{config.station_id}:{uuid4()}"
            )
            result = asyncio.run(
                run_once(config, cups=arguments.cups, idempotency_key=key)
            )
    except (RuntimeError, ValueError) as error:
        parser.exit(1, f"station failed: {error}\n")
    print(json.dumps(result, indent=2))
