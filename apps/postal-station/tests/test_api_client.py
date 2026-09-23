from __future__ import annotations

from datetime import date
from email.message import Message

import pytest

from package_factory import RUN_ID
from package_factory import TASK_ID
from postal_station.api_client import ApiError
from postal_station.api_client import UrllibPostalStationApi
from postal_station.configuration import StationConfig


def config(tmp_path, *, max_package_bytes=1024):
    return StationConfig(
        "https://postal.example.test",
        "station-1",
        "mailroom",
        tmp_path,
        max_package_bytes=max_package_bytes,
    )


async def test_claim_pins_run_token_for_package_access(monkeypatch, tmp_path):
    seen = None

    async def json_request(self, method, path, body=None, *, headers=None):
        nonlocal seen
        seen = method, path, body, headers
        return {
            "id": RUN_ID,
            "station_id": "station-1",
            "site_id": "brussels",
            "claim_token": "run-token",
            "task_ids": [TASK_ID],
            "duplicate": False,
        }

    monkeypatch.setattr(
        "postal_station.api_client.secrets.token_urlsafe", lambda size: "run-token"
    )
    monkeypatch.setattr(UrllibPostalStationApi, "_json", json_request)
    api = UrllibPostalStationApi(config(tmp_path))

    run = await api.claim_run("claim-1", {"items": 10})

    assert run.id == RUN_ID
    assert api._run_headers(RUN_ID) == {"X-Postal-Run-Token": "run-token"}
    assert seen == (
        "POST",
        "/api/v1/postal/local/production-runs/claims",
        {"limits": {"items": 10}},
        {"Idempotency-Key": "claim-1", "X-Postal-Run-Token": "run-token"},
    )


async def test_claim_retry_reuses_the_original_run_token(monkeypatch, tmp_path):
    observed_headers = []

    async def json_request(self, method, path, body=None, *, headers=None):
        headers_value = dict(headers or {})
        observed_headers.append(headers_value)
        if len(observed_headers) == 1:
            raise ApiError("response lost")
        return {
            "id": RUN_ID,
            "station_id": "station-1",
            "site_id": "brussels",
            "claim_token": headers_value["X-Postal-Run-Token"],
            "task_ids": [TASK_ID],
            "duplicate": True,
        }

    monkeypatch.setattr(UrllibPostalStationApi, "_json", json_request)
    api = UrllibPostalStationApi(config(tmp_path))

    with pytest.raises(ApiError, match="response lost"):
        await api.claim_run("claim-1")
    await api.claim_run("claim-1")

    assert (
        observed_headers[0]["X-Postal-Run-Token"]
        == observed_headers[1]["X-Postal-Run-Token"]
    )


async def test_create_handover_batch_sends_replayable_request(monkeypatch, tmp_path):
    seen = None

    async def json_request(self, method, path, body=None, *, headers=None):
        nonlocal seen
        seen = method, path, body, headers
        return {
            "id": "55555555-5555-4555-8555-555555555555",
            "duplicate": False,
        }

    monkeypatch.setattr(UrllibPostalStationApi, "_json", json_request)
    api = UrllibPostalStationApi(config(tmp_path))

    batch = await api.create_handover_batch(
        date(2026, 8, 31), (TASK_ID,), "mock-station:station-1", "batch-1"
    )

    assert batch.id == "55555555-5555-4555-8555-555555555555"
    assert seen == (
        "POST",
        "/api/v1/postal/local/handover-batches",
        {
            "service_date": "2026-08-31",
            "task_ids": [TASK_ID],
            "operator_reference": "mock-station:station-1",
        },
        {"Idempotency-Key": "batch-1"},
    )


async def test_download_reads_case_insensitive_receipt_headers(monkeypatch, tmp_path):
    package = b"package"
    seen = None

    def request(self, method, path, payload, max_response_bytes, headers=None):
        nonlocal seen
        seen = method, path, payload, max_response_bytes, headers
        return package, {
            "x-postal-package-sha256": "0" * 64,
            "x-postal-manifest-sha256": "1" * 64,
            "x-postal-package-bytes": str(len(package)),
        }

    monkeypatch.setattr(UrllibPostalStationApi, "_request", request)

    api = UrllibPostalStationApi(config(tmp_path, max_package_bytes=99))
    api._run_tokens["run/1"] = "run-token"
    downloaded = await api.download_package("run/1")

    assert downloaded.content == package
    assert downloaded.receipt.byte_count == len(package)
    assert seen == (
        "GET",
        "/api/v1/postal/local/production-runs/run%2F1/package",
        None,
        99,
        {"X-Postal-Run-Token": "run-token"},
    )


def test_request_rejects_declared_response_over_size_limit(monkeypatch, tmp_path):
    headers = Message()
    headers["Content-Length"] = "101"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            raise AssertionError("oversized response must not be read")

    response = Response()
    response.headers = headers
    monkeypatch.setattr(
        "postal_station.api_client.urllib.request.urlopen",
        lambda request, timeout: response,
    )

    with pytest.raises(ApiError, match="size limit"):
        UrllibPostalStationApi(config(tmp_path))._request("GET", "/resource", None, 100)


def test_request_rejects_chunked_response_over_size_limit(monkeypatch, tmp_path):
    class Response:
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr(
        "postal_station.api_client.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )

    with pytest.raises(ApiError, match="size limit"):
        UrllibPostalStationApi(config(tmp_path))._request("GET", "/resource", None, 100)
