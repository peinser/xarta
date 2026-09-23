from __future__ import annotations

import secrets

from datetime import UTC
from datetime import date
from datetime import datetime
from uuid import UUID

from sanic import response

from xarta.services.v1.postal.adapters.local.production import build_and_publish_run
from xarta.services.v1.postal.adapters.local.repository import RunLimits
from xarta.services.v1.postal.adapters.local.workflow import PrintAttemptEvent
from xarta.services.v1.postal.adapters.local.workflow import ScanMode


def _identity(request):
    return request.app.ctx.postal_station_registry.authenticate(
        request.headers.get("authorization")
    )


def _json_object(request) -> dict:
    value = request.json
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def _run_token(request) -> str:
    token = request.headers.get("x-postal-run-token")
    if not token:
        raise PermissionError("X-Postal-Run-Token is required")
    return str(token)


async def claim_run(request):
    try:
        identity = _identity(request)
        key = request.headers.get("idempotency-key")
        if not key:
            raise ValueError("Idempotency-Key is required")
        data = _json_object(request)
        if set(data) - {"limits"}:
            raise ValueError("Unknown production claim fields")
        raw_limits = data.get("limits", {})
        if not isinstance(raw_limits, dict):
            raise ValueError("limits must be an object")
        limits = RunLimits(**raw_limits)
        token = request.headers.get("x-postal-run-token") or secrets.token_urlsafe(32)
        run = await request.app.ctx.postal_repository.claim_run(
            station_id=identity.id,
            site_id=identity.site,
            idempotency_key=key,
            claim_token=token,
            limits=limits,
        )
        if run.task_ids and not run.duplicate:
            try:
                await build_and_publish_run(
                    request.app.ctx.postal_repository,
                    request.app.ctx.postal_artifacts,
                    run,
                    station_id=identity.id,
                    site_id=identity.site,
                )
            except Exception:
                await request.app.ctx.postal_repository.release_unpublished_run(run.id)
                raise
        if run.task_ids:
            await request.app.ctx.logger.ainfo(
                "postal_production_run_claimed",
                production_run_id=str(run.id),
                station_id=identity.id,
                site_id=identity.site,
                task_count=len(run.task_ids),
                duplicate=run.duplicate,
            )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except (TypeError, ValueError) as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json(
        {
            "id": str(run.id),
            "station_id": run.station_id,
            "site_id": run.site_id,
            "claim_token": token,
            "task_ids": [str(item) for item in run.task_ids],
            "duplicate": run.duplicate,
        },
        status=200 if run.duplicate else 201,
    )


async def active_runs(request, station_id: str):
    try:
        identity = _identity(request)
        request.app.ctx.postal_station_registry.require_station(identity, station_id)
        runs = await request.app.ctx.postal_repository.active_runs(
            station_id, identity.site
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=403)
    return response.json({"runs": runs})


async def production_run(request, run_id: str):
    try:
        identity = _identity(request)
        value = await request.app.ctx.postal_repository.production_run(
            UUID(run_id), identity.id, identity.site
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except (LookupError, ValueError):
        return response.json({"error": "production run not found"}, status=404)
    return response.json(value)


async def download_package(request, run_id: str):
    try:
        identity = _identity(request)
        record = await request.app.ctx.postal_repository.package(
            UUID(run_id), identity.id, _run_token(request)
        )
        content = await request.app.ctx.postal_artifacts.get(record.storage_reference)
        if len(content) != record.byte_count:
            raise RuntimeError("Persisted package size does not match metadata")
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except (LookupError, ValueError):
        return response.json({"error": "production package not found"}, status=404)
    return response.raw(
        content,
        content_type="application/zip",
        headers={
            "X-Postal-Package-SHA256": record.checksum,
            "X-Postal-Manifest-SHA256": record.manifest_checksum,
            "X-Postal-Package-Bytes": str(record.byte_count),
        },
    )


async def acknowledge_package(request, run_id: str):
    try:
        identity = _identity(request)
        data = _json_object(request)
        if set(data) != {"package_sha256", "manifest_sha256", "byte_count"}:
            raise ValueError("Invalid package acknowledgement fields")
        idempotency_key = request.headers.get("idempotency-key")
        if not idempotency_key:
            raise ValueError("Idempotency-Key is required")
        duplicate = await request.app.ctx.postal_repository.acknowledge_package(
            UUID(run_id),
            identity.id,
            _run_token(request),
            idempotency_key,
            data["package_sha256"],
            data["manifest_sha256"],
            data["byte_count"],
        )
        await request.app.ctx.logger.ainfo(
            "postal_production_package_acknowledged",
            production_run_id=run_id,
            station_id=identity.id,
            duplicate=duplicate,
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    except (TypeError, ValueError) as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json({"duplicate": duplicate})


async def create_print_attempt(request, job_id: str):
    try:
        identity = _identity(request)
        data = _json_object(request)
        if set(data) != {"generation", "rendered_sha256", "rendered_byte_count"}:
            raise ValueError("Invalid print attempt fields")
        if (
            not isinstance(data["generation"], int)
            or isinstance(data["generation"], bool)
            or not isinstance(data["rendered_byte_count"], int)
            or isinstance(data["rendered_byte_count"], bool)
            or data["generation"] < 1
            or data["rendered_byte_count"] < 1
        ):
            raise ValueError(
                "Print attempt generation and rendered byte count must be integers"
            )
        rendered_sha256 = data["rendered_sha256"]
        if (
            not isinstance(rendered_sha256, str)
            or len(rendered_sha256) != 64
            or any(character not in "0123456789abcdef" for character in rendered_sha256)
        ):
            raise ValueError("rendered_sha256 must be a lowercase SHA-256 digest")
        event_identity = request.headers.get("idempotency-key")
        if not event_identity:
            raise ValueError("Idempotency-Key is required")
        attempt = await request.app.ctx.postal_repository.create_print_attempt(
            UUID(job_id),
            identity.id,
            _run_token(request),
            data["generation"],
            event_identity,
            rendered_sha256,
            data["rendered_byte_count"],
        )
        await request.app.ctx.logger.ainfo(
            "postal_print_attempt_created",
            flow_id=attempt["flow_id"],
            correlation_id=attempt["correlation_id"],
            node_execution_id=attempt["node_execution_id"],
            operation_id=attempt["operation_id"],
            station_id=identity.id,
            production_task_id=attempt["task_id"],
            print_job_id=job_id,
            print_attempt_id=attempt["id"],
            generation=attempt["generation"],
            attempt=attempt["attempt"],
            previous_state=attempt["previous_state"],
            next_state=attempt["next_state"],
            duplicate=attempt["duplicate"],
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json(
        {
            key: attempt[key]
            for key in (
                "id",
                "job_id",
                "generation",
                "attempt",
                "job_name",
                "rendered_sha256",
                "rendered_byte_count",
            )
        },
        status=201,
    )


async def report_print_event(request, attempt_id: str):
    try:
        identity = _identity(request)
        data = _json_object(request)
        if set(data) - {"event", "printer_job_id", "detail"}:
            raise ValueError("Unknown print event fields")
        event_name = data.get("event")
        if not isinstance(event_name, str):
            raise ValueError("Invalid print event")
        event = {
            "completed": PrintAttemptEvent.COMPLETED,
            "failed": PrintAttemptEvent.FAILED,
            "uncertain": PrintAttemptEvent.RESULT_UNKNOWN,
        }.get(event_name)
        if event is None:
            raise ValueError("Invalid print event")
        error = {"detail": data["detail"]} if data.get("detail") else None
        transition = await request.app.ctx.postal_repository.report_print_event(
            UUID(attempt_id),
            identity.id,
            event,
            data.get("printer_job_id"),
            error,
        )
        level = "awarning" if event_name in {"failed", "uncertain"} else "ainfo"
        await getattr(request.app.ctx.logger, level)(
            "postal_print_state_changed",
            flow_id=transition["flow_id"],
            correlation_id=transition["correlation_id"],
            node_execution_id=transition["node_execution_id"],
            operation_id=transition["operation_id"],
            station_id=identity.id,
            production_task_id=transition["task_id"],
            print_job_id=transition["job_id"],
            print_attempt_id=attempt_id,
            generation=transition["generation"],
            previous_state=transition["previous_state"],
            next_state=transition["next_state"],
            duplicate=transition["duplicate"],
            external_printer_reference_present=data.get("printer_job_id") is not None,
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json({"accepted": True})


async def production_scan(request):
    try:
        identity = _identity(request)
        data = _json_object(request)
        if set(data) - {
            "station_id",
            "barcode",
            "mode",
            "generation",
            "handover_batch_id",
            "occurred_at",
        }:
            raise ValueError("Unknown production scan fields")
        request.app.ctx.postal_station_registry.require_station(
            identity, data.get("station_id")
        )
        mode = ScanMode(data["mode"])
        batch = (
            UUID(data["handover_batch_id"]) if data.get("handover_batch_id") else None
        )
        occurred = (
            datetime.fromisoformat(data["occurred_at"])
            if data.get("occurred_at")
            else datetime.now(UTC)
        )
        if occurred.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        target, applied = await request.app.ctx.postal_service.scan(
            station_id=identity.id,
            site_id=identity.site,
            barcode=data["barcode"],
            mode=mode,
            occurred_at=occurred,
            handover_batch_id=batch,
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=403)
    except LookupError as ex:
        return response.json({"error": str(ex)}, status=404)
    except (KeyError, TypeError, ValueError) as ex:
        return response.json({"error": str(ex)}, status=400)
    stage = {
        ScanMode.START_PRODUCTION: "processing",
        ScanMode.READY_FOR_HANDOVER: "prepared",
        ScanMode.CONFIRM_HANDOVER: "handed_over",
    }[mode]
    return response.json(
        {
            "task_id": str(target.task_id),
            "mode": mode.value,
            "stage": stage,
            "duplicate": applied.duplicate,
        }
    )


async def create_handover_batch(request):
    try:
        identity = _identity(request)
        idempotency_key = request.headers.get("idempotency-key")
        if not idempotency_key:
            raise ValueError("Idempotency-Key is required")
        data = _json_object(request)
        if set(data) != {"service_date", "task_ids", "operator_reference"}:
            raise ValueError("Invalid handover batch fields")
        batch_id, duplicate = (
            await request.app.ctx.postal_repository.create_handover_batch(
                station_id=identity.id,
                site_id=identity.site,
                service_date=date.fromisoformat(data["service_date"]),
                task_ids=tuple(UUID(item) for item in data["task_ids"]),
                operator_reference=data["operator_reference"],
                idempotency_key=idempotency_key,
            )
        )
        await request.app.ctx.logger.ainfo(
            "postal_handover_batch_created",
            handover_batch_id=str(batch_id),
            station_id=identity.id,
            site_id=identity.site,
            service_date=data["service_date"],
            task_count=len(data["task_ids"]),
            duplicate=duplicate,
        )
    except PermissionError as ex:
        return response.json({"error": str(ex)}, status=401)
    except ValueError as ex:
        return response.json({"error": str(ex)}, status=400)
    return response.json(
        {"id": str(batch_id), "duplicate": duplicate},
        status=200 if duplicate else 201,
    )
