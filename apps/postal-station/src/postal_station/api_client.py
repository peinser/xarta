"""Async station API abstraction and urllib implementation."""

from __future__ import annotations

import asyncio
import json
import secrets
import urllib.error
import urllib.request

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Protocol
from urllib.parse import quote

from postal_station.configuration import StationConfig
from postal_station.models import ActiveRun
from postal_station.models import ClaimedRun
from postal_station.models import HandoverBatch
from postal_station.models import PackageReceipt
from postal_station.models import PrintAttempt
from postal_station.models import PrintAttemptEvent
from postal_station.models import ScanAcknowledgement
from postal_station.models import ScanMode


class ApiError(RuntimeError):
    """The server did not acknowledge a station request successfully."""


@dataclass(frozen=True, slots=True)
class DownloadedPackage:
    content: bytes
    receipt: PackageReceipt


class PostalStationApi(Protocol):
    async def claim_run(
        self, idempotency_key: str, limits: dict[str, int] | None = None
    ) -> ClaimedRun: ...

    async def active_runs(self, station_id: str) -> tuple[ActiveRun, ...]: ...

    async def download_package(self, run_id: str) -> DownloadedPackage: ...

    async def acknowledge_package(
        self, run_id: str, receipt: PackageReceipt
    ) -> None: ...

    async def create_print_attempt(
        self,
        run_id: str,
        job_id: str,
        generation: int,
        rendered_sha256: str,
        rendered_byte_count: int,
    ) -> PrintAttempt: ...

    async def report_print_event(
        self,
        attempt_id: str,
        event: PrintAttemptEvent,
        *,
        printer_job_id: str | None = None,
        detail: str | None = None,
    ) -> None: ...

    async def submit_scan(
        self,
        station_id: str,
        barcode: str,
        mode: ScanMode,
        *,
        handover_batch_id: str | None = None,
    ) -> ScanAcknowledgement: ...

    async def create_handover_batch(
        self,
        service_date: date,
        task_ids: tuple[str, ...],
        operator_reference: str,
        idempotency_key: str,
    ) -> HandoverBatch: ...


@dataclass(slots=True)
class UrllibPostalStationApi:
    config: StationConfig
    _run_tokens: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _claim_tokens: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    async def claim_run(
        self, idempotency_key: str, limits: dict[str, int] | None = None
    ) -> ClaimedRun:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        token = self._claim_tokens.get(idempotency_key)
        if token is None:
            token = secrets.token_urlsafe(32)
            self._claim_tokens[idempotency_key] = token
        value = await self._json(
            "POST",
            "/api/v1/postal/local/production-runs/claims",
            {"limits": limits or {}},
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Postal-Run-Token": token,
            },
        )
        try:
            run = ClaimedRun.from_dict(value)
        except ValueError as error:
            raise ApiError(f"invalid claim response: {error}") from error
        if run.claim_token != token:
            raise ApiError("server returned a different run claim token")
        self._run_tokens[run.id] = token
        del self._claim_tokens[idempotency_key]
        return run

    async def active_runs(self, station_id: str) -> tuple[ActiveRun, ...]:
        value = await self._json(
            "GET",
            f"/api/v1/postal/local/stations/{quote(station_id, safe='')}/active-runs",
        )
        if (
            not isinstance(value, dict)
            or set(value) != {"runs"}
            or not isinstance(value["runs"], list)
        ):
            raise ApiError("invalid active-runs response")
        try:
            return tuple(ActiveRun.from_dict(item) for item in value["runs"])
        except ValueError as error:
            raise ApiError(f"invalid active-runs response: {error}") from error

    async def download_package(self, run_id: str) -> DownloadedPackage:
        content, headers = await asyncio.to_thread(
            self._request,
            "GET",
            f"/api/v1/postal/local/production-runs/{quote(run_id, safe='')}/package",
            None,
            self.config.max_package_bytes,
            self._run_headers(run_id),
        )
        try:
            receipt = PackageReceipt(
                headers["x-postal-package-sha256"],
                headers["x-postal-manifest-sha256"],
                int(headers["x-postal-package-bytes"]),
            )
        except (KeyError, ValueError) as error:
            raise ApiError(
                "package response is missing valid receipt metadata"
            ) from error
        return DownloadedPackage(content, receipt)

    async def acknowledge_package(self, run_id: str, receipt: PackageReceipt) -> None:
        await self._json(
            "POST",
            f"/api/v1/postal/local/production-runs/{quote(run_id, safe='')}/package-acknowledgements",
            {
                "package_sha256": receipt.package_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "byte_count": receipt.byte_count,
            },
            headers={
                **self._run_headers(run_id),
                "Idempotency-Key": f"package-ack:{run_id}:{receipt.package_sha256}",
            },
        )

    async def create_print_attempt(
        self,
        run_id: str,
        job_id: str,
        generation: int,
        rendered_sha256: str,
        rendered_byte_count: int,
    ) -> PrintAttempt:
        value = await self._json(
            "POST",
            f"/api/v1/postal/local/print-jobs/{quote(job_id, safe='')}/attempts",
            {
                "generation": generation,
                "rendered_sha256": rendered_sha256,
                "rendered_byte_count": rendered_byte_count,
            },
            headers={
                **self._run_headers(run_id),
                "Idempotency-Key": f"print-attempt:{job_id}:{generation}",
            },
        )
        try:
            return PrintAttempt.from_dict(value)
        except ValueError as error:
            raise ApiError(f"invalid print-attempt response: {error}") from error

    async def report_print_event(
        self,
        attempt_id: str,
        event: PrintAttemptEvent,
        *,
        printer_job_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        body: dict[str, object] = {"event": event.value}
        if printer_job_id is not None:
            body["printer_job_id"] = printer_job_id
        if detail is not None:
            body["detail"] = detail
        await self._json(
            "POST",
            f"/api/v1/postal/local/print-attempts/{quote(attempt_id, safe='')}/events",
            body,
        )

    async def submit_scan(
        self,
        station_id: str,
        barcode: str,
        mode: ScanMode,
        *,
        handover_batch_id: str | None = None,
    ) -> ScanAcknowledgement:
        body = {"station_id": station_id, "barcode": barcode, "mode": mode.value}
        if handover_batch_id is not None:
            body["handover_batch_id"] = handover_batch_id
        value = await self._json(
            "POST",
            "/api/v1/postal/local/production-scans",
            body,
        )
        try:
            return ScanAcknowledgement.from_dict(value)
        except ValueError as error:
            raise ApiError(f"invalid scan acknowledgement: {error}") from error

    async def create_handover_batch(
        self,
        service_date: date,
        task_ids: tuple[str, ...],
        operator_reference: str,
        idempotency_key: str,
    ) -> HandoverBatch:
        if not task_ids:
            raise ValueError("task_ids must not be empty")
        if not operator_reference:
            raise ValueError("operator_reference must not be empty")
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        value = await self._json(
            "POST",
            "/api/v1/postal/local/handover-batches",
            {
                "service_date": service_date.isoformat(),
                "task_ids": list(task_ids),
                "operator_reference": operator_reference,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
        try:
            return HandoverBatch.from_dict(value)
        except ValueError as error:
            raise ApiError(f"invalid handover-batch response: {error}") from error

    def _run_headers(self, run_id: str) -> dict[str, str]:
        try:
            return {"X-Postal-Run-Token": self._run_tokens[run_id]}
        except KeyError as error:
            raise ApiError("run claim token is unavailable") from error

    async def _json(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> object:
        payload = (
            None
            if body is None
            else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )
        content, _headers = await asyncio.to_thread(
            self._request, method, path, payload, 1_048_576, headers
        )
        if not content:
            return None
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError("server returned invalid JSON") from error

    def _request(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        max_response_bytes: int,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.config.api_token is not None:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            self.config.api_url + path, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise ApiError(
                            "server returned an invalid Content-Length"
                        ) from error
                    if declared_length < 0 or declared_length > max_response_bytes:
                        raise ApiError(
                            "server response exceeds the configured size limit"
                        )
                content = response.read(max_response_bytes + 1)
                if len(content) > max_response_bytes:
                    raise ApiError("server response exceeds the configured size limit")
                response_headers = {
                    name.lower(): value for name, value in response.headers.items()
                }
                return content, response_headers
        except urllib.error.HTTPError as error:
            raise ApiError(f"server rejected request with HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise ApiError(f"station API request failed: {error.reason}") from error
