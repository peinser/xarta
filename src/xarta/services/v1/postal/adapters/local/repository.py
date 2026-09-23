from __future__ import annotations

import hashlib
import uuid

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID
from uuid import uuid5

import orjson

from xarta.services.v1.postal.adapters.local.capacity import CapacityDecision
from xarta.services.v1.postal.adapters.local.capacity import CapacityOverflow
from xarta.services.v1.postal.adapters.local.capacity import CapacityPool
from xarta.services.v1.postal.adapters.local.capacity import capacity_decision
from xarta.services.v1.postal.adapters.local.workflow import PhysicalState
from xarta.services.v1.postal.adapters.local.workflow import PrintAttemptEvent
from xarta.services.v1.postal.adapters.local.workflow import PrintJob
from xarta.services.v1.postal.adapters.local.workflow import PrintJobKind
from xarta.services.v1.postal.adapters.local.workflow import PrintJobState
from xarta.services.v1.postal.adapters.local.workflow import ScanMode
from xarta.services.v1.postal.adapters.local.workflow import printer_job_name
from xarta.services.v1.postal.adapters.local.workflow import reduce_print_state
from xarta.services.v1.postal.adapters.local.workflow import reduce_scan

if TYPE_CHECKING:
    from collections.abc import Mapping

    import asyncpg  # type: ignore[import-untyped]

    from xarta.services.v1.postal.adapters.local.artifacts import StoredDocument
    from xarta.tracking import ReductionTransactionContext


IDENTITY_NAMESPACE = UUID("031c5b3d-0707-522d-ac0f-a2531e2ea574")


def _jsonb_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = orjson.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Persisted PostgreSQL JSONB value must be an object")
    return value


@dataclass(frozen=True, slots=True)
class RunLimits:
    items: int = 100
    jobs: int = 200
    pages: int = 5_000
    sheets: int = 3_000
    bytes: int = 500_000_000

    def __post_init__(self) -> None:
        if min(self.items, self.jobs, self.pages, self.sheets, self.bytes) < 1:
            raise ValueError("Production run limits must be positive")


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    id: UUID
    station_id: str
    site_id: str
    claim_token: str
    task_ids: tuple[UUID, ...]
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class PackageRecord:
    run_id: UUID
    storage_reference: str
    checksum: str
    manifest_checksum: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class ScanTarget:
    operation_id: UUID
    adapter: str
    flow_id: UUID
    correlation_id: UUID
    node_execution_id: UUID
    task_id: UUID
    generation: int
    service: str
    physical_state: PhysicalState
    handover_batch_id: UUID | None


@dataclass(frozen=True, slots=True)
class RunPackageItem:
    operation_id: UUID
    task_id: UUID
    sequence: int
    generation: int
    plan_digest: str
    plan: Mapping[str, Any]
    sources: tuple[tuple[int, str, str, str, int, int], ...]


class LocalPostalRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def create_submission(
        self,
        *,
        tracked_operation_id: UUID,
        service: str,
        site_id: str,
        service_date: date,
        plan: Mapping[str, Any],
        plan_digest: str,
        documents: tuple[StoredDocument, ...],
        capacity_pool: CapacityPool,
    ) -> tuple[UUID, CapacityDecision]:
        operation_id = uuid5(IDENTITY_NAMESPACE, f"operation:{tracked_operation_id}")
        plan_id = uuid5(operation_id, f"plan:{plan_digest}")
        task_id = uuid5(operation_id, "production-task:v1")
        async with self.pool.acquire() as connection, connection.transaction():
            operation_pk = await connection.fetchval(
                """
                INSERT INTO postal_local.operations (id, tracked_operation_id, service)
                SELECT $1, tracked._id, $2 FROM public.tracked_operations tracked
                WHERE tracked.id = $3
                ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
                RETURNING _id
                """,
                operation_id,
                service,
                tracked_operation_id,
            )
            if operation_pk is None:
                raise LookupError("Tracked postal operation does not exist")
            plan_pk = await connection.fetchval(
                """
                INSERT INTO postal_local.production_plans (id, operation_id, digest, plan)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (operation_id) DO UPDATE SET operation_id = EXCLUDED.operation_id
                RETURNING _id
                """,
                plan_id,
                operation_pk,
                plan_digest,
                orjson.dumps(dict(plan)).decode(),
            )
            existing_digest = await connection.fetchval(
                "SELECT digest FROM postal_local.production_plans WHERE _id = $1",
                plan_pk,
            )
            if existing_digest != plan_digest:
                raise ValueError(
                    "Postal operation is already bound to another production plan"
                )
            task_pk = await connection.fetchval(
                """
                INSERT INTO postal_local.production_tasks
                    (id, operation_id, production_plan_id, site_id, service_date)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (operation_id) DO UPDATE SET operation_id = EXCLUDED.operation_id
                RETURNING _id
                """,
                task_id,
                operation_pk,
                plan_pk,
                site_id,
                service_date,
            )
            for document in documents:
                await connection.execute(
                    """
                    INSERT INTO postal_local.artifacts
                        (id, operation_id, kind, storage_reference, sha256, size,
                         content_type, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, 'application/pdf', $7::jsonb)
                    ON CONFLICT (operation_id, kind, generation) DO NOTHING
                    """,
                    document.id,
                    operation_pk,
                    f"source:{document.ordinal}",
                    document.storage_reference,
                    document.sha256,
                    document.size,
                    orjson.dumps(
                        {
                            "ordinal": document.ordinal,
                            "role": document.role,
                            "source": document.source,
                            "source_id": document.source_id,
                            "source_version": document.source_version,
                            "page_count": document.page_count,
                        }
                    ).decode(),
                )
            decision = await self._reserve_capacity(
                connection, task_pk, task_id, service_date, capacity_pool
            )
            assignment = (
                "pending" if decision is CapacityDecision.ADMIT else "waiting_capacity"
            )
            await connection.execute(
                """
                UPDATE postal_local.production_tasks SET assignment_state = $2, updated_at = now()
                WHERE _id = $1 AND assignment_state IN ('preparing', 'waiting_capacity')
                """,
                task_pk,
                assignment,
            )
        return operation_id, decision

    async def reject_capacity(
        self, connection: Any, tracked_operation_id: UUID
    ) -> None:
        updated = await connection.fetchval(
            """
            UPDATE postal_local.operations operation SET state = 'failed', version = version + 1,
                updated_at = now()
            FROM public.tracked_operations tracked
            WHERE operation.tracked_operation_id = tracked._id AND tracked.id = $1
            RETURNING operation._id
            """,
            tracked_operation_id,
        )
        if updated is None:
            raise LookupError("Local postal operation does not exist")
        await connection.execute(
            """
            UPDATE postal_local.production_tasks task SET assignment_state = 'cancelled',
                updated_at = now()
            FROM postal_local.operations operation
            WHERE task.operation_id = operation._id AND operation._id = $1
            """,
            updated,
        )

    async def _reserve_capacity(
        self,
        connection: Any,
        task_pk: int,
        task_id: UUID,
        service_date: date,
        pool: CapacityPool,
    ) -> CapacityDecision:
        day_id = uuid5(
            IDENTITY_NAMESPACE, f"capacity:{pool.id}:{service_date.isoformat()}"
        )
        await connection.execute(
            """
            INSERT INTO postal_local.capacity_days
                (id, pool_id, service_date, timezone, daily_admission_limit)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            day_id,
            pool.id,
            service_date,
            pool.timezone,
            pool.daily_admission_limit,
        )
        day = await connection.fetchrow(
            """
            SELECT _id, daily_admission_limit, admitted
            FROM postal_local.capacity_days WHERE pool_id = $1 AND service_date = $2
            FOR UPDATE
            """,
            pool.id,
            service_date,
        )
        if day["daily_admission_limit"] != pool.daily_admission_limit:
            raise ValueError(
                "Pinned capacity limit differs from the persisted service day"
            )
        decision = capacity_decision(
            CapacityPool(
                pool.id, pool.timezone, day["daily_admission_limit"], pool.overflow
            ),
            admitted=day["admitted"],
        )
        if decision is not CapacityDecision.ADMIT:
            return decision
        inserted = await connection.fetchval(
            """
            INSERT INTO postal_local.capacity_reservations
                (id, capacity_day_id, production_task_id)
            VALUES ($1, $2, $3) ON CONFLICT (production_task_id) DO NOTHING
            RETURNING _id
            """,
            uuid5(IDENTITY_NAMESPACE, f"capacity-reservation:{task_id}"),
            day["_id"],
            task_pk,
        )
        if inserted is not None:
            await connection.execute(
                "UPDATE postal_local.capacity_days SET admitted = admitted + 1, version = version + 1, updated_at = now() WHERE _id = $1",
                day["_id"],
            )
        return decision

    async def claim_run(
        self,
        *,
        station_id: str,
        site_id: str,
        idempotency_key: str,
        claim_token: str,
        limits: RunLimits,
    ) -> ClaimedRun:
        digest = hashlib.sha256(claim_token.encode()).hexdigest()
        run_id = uuid5(IDENTITY_NAMESPACE, f"run:{station_id}:{idempotency_key}")
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                "SELECT id, claim_token_digest FROM postal_local.production_runs WHERE station_id = $1 AND claim_idempotency_key = $2 FOR UPDATE",
                station_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["claim_token_digest"] != digest:
                    raise PermissionError("Claim token does not match the existing run")
                task_ids = await connection.fetch(
                    """
                    SELECT task.id FROM postal_local.production_run_items item
                    JOIN postal_local.production_tasks task ON task._id = item.production_task_id
                    JOIN postal_local.production_runs run ON run._id = item.production_run_id
                    WHERE run.id = $1 ORDER BY item.sequence
                    """,
                    existing["id"],
                )
                return ClaimedRun(
                    existing["id"],
                    station_id,
                    site_id,
                    claim_token,
                    tuple(row["id"] for row in task_ids),
                    True,
                )
            run_pk = await connection.fetchval(
                """
                INSERT INTO postal_local.production_runs
                    (id, station_id, site_id, claim_idempotency_key, claim_token_digest)
                VALUES ($1, $2, $3, $4, $5) RETURNING _id
                """,
                run_id,
                station_id,
                site_id,
                idempotency_key,
                digest,
            )
            candidates = await connection.fetch(
                """
                SELECT task._id, task.id, task.generation,
                       COALESCE((plan.plan->'quantities'->>'content_pages')::int, 0) pages,
                       COALESCE((plan.plan->'quantities'->>'sheets')::int, 0) sheets,
                       COALESCE((SELECT sum(artifact.size) FROM postal_local.artifacts artifact
                                 WHERE artifact.operation_id = task.operation_id), 0) bytes
                FROM postal_local.production_tasks task
                JOIN postal_local.production_plans plan ON plan._id = task.production_plan_id
                WHERE task.site_id = $1 AND task.assignment_state = 'pending'
                ORDER BY task.priority DESC, task._id
                FOR UPDATE OF task SKIP LOCKED LIMIT $2
                """,
                site_id,
                limits.items,
            )
            selected: list[Any] = []
            pages = sheets = byte_count = 0
            for row in candidates:
                next_pages = pages + row["pages"]
                next_sheets = sheets + row["sheets"]
                next_bytes = byte_count + row["bytes"]
                if (
                    2 * (len(selected) + 1) > limits.jobs
                    or next_pages > limits.pages
                    or next_sheets > limits.sheets
                    or next_bytes > limits.bytes
                ):
                    break
                selected.append(row)
                pages, sheets, byte_count = next_pages, next_sheets, next_bytes
            for sequence, row in enumerate(selected, 1):
                await connection.execute(
                    """
                    INSERT INTO postal_local.production_run_items
                        (id, production_run_id, production_task_id, generation, sequence)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    uuid5(run_id, f"item:{row['id']}:{row['generation']}"),
                    run_pk,
                    row["_id"],
                    row["generation"],
                    sequence,
                )
            if selected:
                await connection.execute(
                    "UPDATE postal_local.production_tasks SET assignment_state = 'claimed', updated_at = now() WHERE _id = ANY($1::bigint[])",
                    [row["_id"] for row in selected],
                )
            else:
                await connection.execute(
                    "UPDATE postal_local.production_runs SET state = 'cancelled', updated_at = now() WHERE _id = $1",
                    run_pk,
                )
        return ClaimedRun(
            run_id,
            station_id,
            site_id,
            claim_token,
            tuple(row["id"] for row in selected),
        )

    async def active_runs(
        self, station_id: str, site_id: str
    ) -> tuple[dict[str, Any], ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, package_acknowledged_at IS NOT NULL package_acknowledged
                FROM postal_local.production_runs
                WHERE station_id = $1 AND site_id = $2 AND state NOT IN ('completed', 'cancelled')
                ORDER BY created_at, _id
                """,
                station_id,
                site_id,
            )
        return tuple(
            {"id": str(row["id"]), "package_acknowledged": row["package_acknowledged"]}
            for row in rows
        )

    async def production_run(
        self, run_id: UUID, station_id: str, site_id: str
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, state, package_checksum, manifest_checksum,
                       package_byte_count, package_acknowledged_at
                FROM postal_local.production_runs
                WHERE id = $1 AND station_id = $2 AND site_id = $3
                """,
                run_id,
                station_id,
                site_id,
            )
            if row is None:
                raise LookupError("Production run not found")
            items = await connection.fetch(
                """
                SELECT task.id task_id, item.sequence, item.generation,
                       job.id job_id, job.state job_state,
                       attempt.id attempt_id, attempt.state attempt_state,
                       attempt.attempt, attempt.rendered_sha256,
                       attempt.rendered_byte_count
                FROM postal_local.production_run_items item
                JOIN postal_local.production_tasks task ON task._id = item.production_task_id
                JOIN postal_local.production_runs run ON run._id = item.production_run_id
                LEFT JOIN postal_local.print_jobs job ON job.production_run_item_id = item._id
                LEFT JOIN LATERAL (
                    SELECT * FROM postal_local.print_attempts value
                    WHERE value.print_job_id = job._id ORDER BY value.attempt DESC LIMIT 1
                ) attempt ON true
                WHERE run.id = $1 ORDER BY item.sequence, job.sequence
                """,
                run_id,
            )
        return {
            "id": str(row["id"]),
            "state": row["state"],
            "package_sha256": row["package_checksum"],
            "manifest_sha256": row["manifest_checksum"],
            "package_byte_count": row["package_byte_count"],
            "package_acknowledged": row["package_acknowledged_at"] is not None,
            "items": [
                {
                    "task_id": str(item["task_id"]),
                    "sequence": item["sequence"],
                    "generation": item["generation"],
                    "job": (
                        None
                        if item["job_id"] is None
                        else {
                            "id": str(item["job_id"]),
                            "state": item["job_state"],
                            "attempt": (
                                None
                                if item["attempt_id"] is None
                                else {
                                    "id": str(item["attempt_id"]),
                                    "number": item["attempt"],
                                    "state": item["attempt_state"],
                                    "rendered_sha256": item["rendered_sha256"],
                                    "rendered_byte_count": item["rendered_byte_count"],
                                }
                            ),
                        }
                    ),
                }
                for item in items
            ],
        }

    async def run_package_items(
        self, run_id: UUID, station_id: str, site_id: str
    ) -> tuple[RunPackageItem, ...]:
        async with self.pool.acquire() as connection:
            run = await connection.fetchrow(
                "SELECT _id FROM postal_local.production_runs WHERE id = $1 AND station_id = $2 AND site_id = $3",
                run_id,
                station_id,
                site_id,
            )
            if run is None:
                raise LookupError("Production run not found")
            rows = await connection.fetch(
                """
                SELECT operation.id operation_id, task.id task_id, item.sequence,
                       item.generation, plan.digest, plan.plan
                FROM postal_local.production_run_items item
                JOIN postal_local.production_tasks task ON task._id = item.production_task_id
                JOIN postal_local.operations operation ON operation._id = task.operation_id
                JOIN postal_local.production_plans plan ON plan._id = task.production_plan_id
                WHERE item.production_run_id = $1 ORDER BY item.sequence
                """,
                run["_id"],
            )
            result = []
            for row in rows:
                artifacts = await connection.fetch(
                    """
                    SELECT (metadata->>'ordinal')::int ordinal, storage_reference,
                           COALESCE(metadata->>'role', 'document') role,
                           sha256, size, (metadata->>'page_count')::int page_count
                    FROM postal_local.artifacts
                    WHERE operation_id = (
                        SELECT _id FROM postal_local.operations WHERE id = $1
                    ) AND kind LIKE 'source:%'
                    ORDER BY (metadata->>'ordinal')::int
                    """,
                    row["operation_id"],
                )
                result.append(
                    RunPackageItem(
                        operation_id=row["operation_id"],
                        task_id=row["task_id"],
                        sequence=row["sequence"],
                        generation=row["generation"],
                        plan_digest=row["digest"],
                        plan=_jsonb_object(row["plan"]),
                        sources=tuple(
                            (
                                item["ordinal"],
                                item["storage_reference"],
                                item["role"],
                                item["sha256"],
                                item["size"],
                                item["page_count"],
                            )
                            for item in artifacts
                        ),
                    )
                )
        return tuple(result)

    async def publish_run_package(
        self,
        *,
        run_id: UUID,
        package_storage_reference: str,
        package_checksum: str,
        manifest_checksum: str,
        package_byte_count: int,
        items: tuple[RunPackageItem, ...],
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            run = await connection.fetchrow(
                "SELECT _id, package_checksum FROM postal_local.production_runs WHERE id = $1 FOR UPDATE",
                run_id,
            )
            if run is None:
                raise LookupError("Production run not found")
            if run["package_checksum"] is not None:
                if run["package_checksum"] != package_checksum:
                    raise ValueError(
                        "Production run already has another immutable package"
                    )
                return
            if not items:
                raise ValueError("A production package requires at least one letter")
            for item in items:
                run_item_pk = await connection.fetchval(
                    """
                    SELECT run_item._id FROM postal_local.production_run_items run_item
                    JOIN postal_local.production_tasks task ON task._id = run_item.production_task_id
                    WHERE run_item.production_run_id = $1 AND task.id = $2
                    """,
                    run["_id"],
                    item.task_id,
                )
                print_settings = item.plan["print"]
                settings = {
                    "color_mode": print_settings["color_mode"],
                    "sides": print_settings["sides"],
                }
                await connection.execute(
                    """
                    INSERT INTO postal_local.print_jobs
                        (id, production_run_item_id, sequence, kind, generation, settings)
                    VALUES ($1, $2, $3, 'letter', $4, $5::jsonb)
                    """,
                    uuid5(item.task_id, f"print-job:{item.generation}:letter"),
                    run_item_pk,
                    item.sequence,
                    item.generation,
                    orjson.dumps(settings).decode(),
                )
                await connection.execute(
                    "UPDATE postal_local.production_tasks SET assignment_state = 'package_ready', updated_at = now() WHERE id = $1",
                    item.task_id,
                )
            await connection.execute(
                """
                UPDATE postal_local.production_runs SET state = 'package_ready',
                    package_storage_reference = $2, package_checksum = $3,
                    manifest_checksum = $4, package_byte_count = $5, updated_at = now()
                WHERE _id = $1
                """,
                run["_id"],
                package_storage_reference,
                package_checksum,
                manifest_checksum,
                package_byte_count,
            )

    async def release_unpublished_run(self, run_id: UUID) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            run = await connection.fetchrow(
                "SELECT _id, package_checksum FROM postal_local.production_runs WHERE id = $1 FOR UPDATE",
                run_id,
            )
            if run is None or run["package_checksum"] is not None:
                return
            await connection.execute(
                """
                UPDATE postal_local.production_tasks task SET assignment_state = 'pending', updated_at = now()
                FROM postal_local.production_run_items item
                WHERE item.production_run_id = $1 AND item.production_task_id = task._id
                  AND task.assignment_state = 'claimed'
                """,
                run["_id"],
            )
            await connection.execute(
                "DELETE FROM postal_local.production_run_items WHERE production_run_id = $1",
                run["_id"],
            )
            await connection.execute(
                "UPDATE postal_local.production_runs SET state = 'cancelled', updated_at = now() WHERE _id = $1",
                run["_id"],
            )

    async def package(self, run_id: UUID, station_id: str, token: str) -> PackageRecord:
        digest = hashlib.sha256(token.encode()).hexdigest()
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT run.id, run.package_checksum, run.manifest_checksum,
                        run.package_byte_count, run.package_storage_reference storage_reference
                FROM postal_local.production_runs run
                WHERE run.id = $1 AND run.station_id = $2 AND run.claim_token_digest = $3
                  AND run.package_storage_reference IS NOT NULL
                """,
                run_id,
                station_id,
                digest,
            )
        if row is None:
            raise LookupError("Production package not found")
        return PackageRecord(
            row["id"],
            row["storage_reference"],
            row["package_checksum"],
            row["manifest_checksum"],
            row["package_byte_count"],
        )

    async def acknowledge_package(
        self,
        run_id: UUID,
        station_id: str,
        token: str,
        idempotency_key: str,
        checksum: str,
        manifest_checksum: str,
        byte_count: int,
    ) -> bool:
        digest = hashlib.sha256(token.encode()).hexdigest()
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM postal_local.production_runs WHERE id = $1 AND station_id = $2 AND claim_token_digest = $3 FOR UPDATE",
                run_id,
                station_id,
                digest,
            )
            if row is None:
                raise LookupError("Production run not found")
            if (
                row["package_checksum"],
                row["manifest_checksum"],
                row["package_byte_count"],
            ) != (checksum, manifest_checksum, byte_count):
                raise ValueError("Package acknowledgement metadata does not match")
            if row["package_acknowledgement_key"] is not None:
                if row["package_acknowledgement_key"] != idempotency_key:
                    raise ValueError("Production package is already acknowledged")
                return True
            await connection.execute(
                """
                UPDATE postal_local.production_runs SET state = 'package_acknowledged',
                    package_acknowledgement_key = $2, package_acknowledged_at = now(), updated_at = now()
                WHERE _id = $1
                """,
                row["_id"],
                idempotency_key,
            )
            await connection.execute(
                """
                UPDATE postal_local.production_tasks task SET assignment_state = 'package_acknowledged', updated_at = now()
                FROM postal_local.production_run_items item
                WHERE item.production_run_id = $1 AND item.production_task_id = task._id
                """,
                row["_id"],
            )
        return False

    async def create_handover_batch(
        self,
        *,
        station_id: str,
        site_id: str,
        service_date: date,
        task_ids: tuple[UUID, ...],
        operator_reference: str | None,
        idempotency_key: str,
    ) -> tuple[UUID, bool]:
        if not task_ids:
            raise ValueError("Handover batch requires at least one task")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Handover batch tasks must be unique")
        if not idempotency_key:
            raise ValueError("Handover batch idempotency key is required")
        batch_id = uuid5(IDENTITY_NAMESPACE, f"handover:{station_id}:{idempotency_key}")
        async with self.pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchrow(
                """
                INSERT INTO postal_local.handover_batches
                    (id, site_id, service_date, operator_reference)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (id) DO NOTHING
                RETURNING _id, id
                """,
                batch_id,
                site_id,
                service_date,
                operator_reference,
            )
            if inserted is None:
                existing = await connection.fetchrow(
                    """
                    SELECT batch.id, batch.site_id, batch.service_date,
                           batch.operator_reference,
                           array_agg(task.id ORDER BY task.id) task_ids
                    FROM postal_local.handover_batches batch
                    JOIN postal_local.handover_batch_items item
                      ON item.handover_batch_id = batch._id
                    JOIN postal_local.production_tasks task
                      ON task._id = item.production_task_id
                    WHERE batch.id = $1
                    GROUP BY batch._id
                    """,
                    batch_id,
                )
                if existing is None:
                    raise RuntimeError("Handover batch idempotency conflict was lost")
                if (
                    existing["site_id"] != site_id
                    or existing["service_date"] != service_date
                    or existing["operator_reference"] != operator_reference
                    or tuple(existing["task_ids"]) != tuple(sorted(task_ids))
                ):
                    raise ValueError(
                        "Handover batch idempotency key was used for another request"
                    )
                return existing["id"], True
            batch_pk = inserted["_id"]
            rows = await connection.fetch(
                """
                SELECT _id, id, generation FROM postal_local.production_tasks
                WHERE id = ANY($1::uuid[]) AND site_id = $2
                  AND physical_state = 'ready_for_handover'
                ORDER BY _id FOR UPDATE
                """,
                list(task_ids),
                site_id,
            )
            if len(rows) != len(set(task_ids)):
                raise ValueError("Handover tasks must exist at the site and be ready")
            for sequence, row in enumerate(rows, 1):
                await connection.execute(
                    """
                    INSERT INTO postal_local.handover_batch_items
                        (id, handover_batch_id, production_task_id, generation, sequence)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    uuid5(batch_id, f"item:{row['id']}"),
                    batch_pk,
                    row["_id"],
                    row["generation"],
                    sequence,
                )
        return batch_id, False

    async def scan_target(
        self, barcode: str, station_id: str, site_id: str
    ) -> ScanTarget:
        try:
            task_id = UUID(barcode)
        except ValueError as ex:
            raise ValueError("Invalid postal traveller barcode") from ex
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT tracked.id operation_id, tracked.adapter,
                       execution.flow_id, execution.correlation_id,
                       execution.id node_execution_id,
                       task.id task_id, task.generation, operation.service,
                       task.physical_state, batch.id handover_batch_id
                FROM postal_local.production_tasks task
                JOIN postal_local.operations operation ON operation._id = task.operation_id
                JOIN public.tracked_operations tracked ON tracked._id = operation.tracked_operation_id
                JOIN public.node_executions execution ON execution._id = tracked.node_execution_id
                LEFT JOIN postal_local.handover_batch_items item ON item.production_task_id = task._id
                LEFT JOIN postal_local.handover_batches batch ON batch._id = item.handover_batch_id
                WHERE task.id = $1 AND task.site_id = $2
                  AND EXISTS (SELECT 1 FROM postal_local.production_run_items run_item
                              JOIN postal_local.production_runs run ON run._id = run_item.production_run_id
                              WHERE run_item.production_task_id = task._id AND run.station_id = $3)
                """,
                task_id,
                site_id,
                station_id,
            )
        if row is None:
            raise LookupError("Postal production task not found")
        return ScanTarget(
            operation_id=row["operation_id"],
            adapter=row["adapter"],
            flow_id=row["flow_id"],
            correlation_id=row["correlation_id"],
            node_execution_id=row["node_execution_id"],
            task_id=row["task_id"],
            generation=row["generation"],
            service=row["service"],
            physical_state=PhysicalState(row["physical_state"]),
            handover_batch_id=row["handover_batch_id"],
        )

    async def apply_scan(
        self,
        connection: Any,
        context: ReductionTransactionContext,
        *,
        target: ScanTarget,
        mode: ScanMode,
        station_id: str,
        occurred_at: datetime,
        handover_batch_id: UUID | None,
    ) -> None:
        row = await connection.fetchrow(
            "SELECT _id, generation, physical_state, operation_id FROM postal_local.production_tasks WHERE id = $1 FOR UPDATE",
            target.task_id,
        )
        if row is None:
            raise LookupError("Postal production task not found")
        batch_pk = None
        if handover_batch_id is not None:
            batch_pk = await connection.fetchval(
                """
                SELECT batch._id FROM postal_local.handover_batches batch
                JOIN postal_local.handover_batch_items item
                  ON item.handover_batch_id = batch._id
                WHERE batch.id = $1 AND item.production_task_id = $2
                  AND item.generation = $3
                FOR UPDATE OF batch, item
                """,
                handover_batch_id,
                row["_id"],
                row["generation"],
            )
            if batch_pk is None:
                raise LookupError("Task is not assigned to this handover batch")
        existing = await connection.fetchval(
            """
            SELECT 1 FROM postal_local.production_events WHERE production_task_id = $1
              AND event_type = $2 AND generation = $3
              AND handover_batch_id IS NOT DISTINCT FROM $4
            """,
            row["_id"],
            mode.value,
            row["generation"],
            batch_pk,
        )
        reduction = reduce_scan(
            PhysicalState(row["physical_state"]),
            mode,
            already_recorded=existing is not None,
            handover_batch_matches=(
                batch_pk is not None if mode is ScanMode.CONFIRM_HANDOVER else True
            ),
            generation=target.generation,
            current_generation=row["generation"],
        )
        if reduction.duplicate:
            return
        await connection.execute(
            """
            INSERT INTO postal_local.production_events
                (id, production_task_id, event_type, generation, handover_batch_id, station_id, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            uuid5(target.task_id, context.external_event_id),
            row["_id"],
            mode.value,
            row["generation"],
            batch_pk,
            station_id,
            occurred_at,
        )
        await connection.execute(
            "UPDATE postal_local.production_tasks SET physical_state = $2, updated_at = now() WHERE _id = $1",
            row["_id"],
            reduction.state.value,
        )
        if mode is ScanMode.CONFIRM_HANDOVER:
            await connection.execute(
                "UPDATE postal_local.production_tasks SET assignment_state = 'completed', updated_at = now() WHERE _id = $1",
                row["_id"],
            )
            await connection.execute(
                """
                UPDATE postal_local.handover_batches batch
                SET state = 'completed', completed_at = now()
                WHERE batch._id = $1
                  AND NOT EXISTS (
                      SELECT 1 FROM postal_local.handover_batch_items item
                      JOIN postal_local.production_tasks task
                        ON task._id = item.production_task_id
                      WHERE item.handover_batch_id = batch._id
                        AND task.physical_state <> 'handed_over'
                  )
                """,
                batch_pk,
            )
            await connection.execute(
                """
                UPDATE postal_local.production_runs run
                SET state = 'completed', updated_at = now()
                WHERE run._id IN (
                    SELECT item.production_run_id
                    FROM postal_local.production_run_items item
                    WHERE item.production_task_id = $1
                )
                  AND NOT EXISTS (
                      SELECT 1 FROM postal_local.production_run_items item
                      JOIN postal_local.production_tasks task
                        ON task._id = item.production_task_id
                      WHERE item.production_run_id = run._id
                        AND task.assignment_state <> 'completed'
                  )
                """,
                row["_id"],
            )
        operation_state = {
            ScanMode.START_PRODUCTION: "in_production",
            ScanMode.READY_FOR_HANDOVER: "prepared",
            ScanMode.CONFIRM_HANDOVER: "handed_over",
        }[mode]
        await connection.execute(
            "UPDATE postal_local.operations SET state = $2, version = version + 1, updated_at = now() WHERE _id = $1",
            row["operation_id"],
            operation_state,
        )

    async def create_print_attempt(
        self,
        job_id: UUID,
        station_id: str,
        token: str,
        generation: int,
        event_identity: str,
        rendered_sha256: str,
        rendered_byte_count: int,
    ) -> dict[str, Any]:
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT job._id, job.id, job.kind, job.generation, job.state,
                       item.sequence, run.id run_id, task.id task_id,
                       tracked.id operation_id, execution.flow_id,
                       execution.correlation_id, execution.id node_execution_id
                FROM postal_local.print_jobs job
                JOIN postal_local.production_run_items item ON item._id = job.production_run_item_id
                JOIN postal_local.production_runs run ON run._id = item.production_run_id
                JOIN postal_local.production_tasks task ON task._id = item.production_task_id
                JOIN postal_local.operations local ON local._id = task.operation_id
                JOIN public.tracked_operations tracked ON tracked._id = local.tracked_operation_id
                JOIN public.node_executions execution ON execution._id = tracked.node_execution_id
                WHERE job.id = $1 AND run.station_id = $2
                  AND run.claim_token_digest = $3 FOR UPDATE OF job
                """,
                job_id,
                station_id,
                token_digest,
            )
            if row is None:
                raise LookupError("Print job not found")
            if row["generation"] != generation:
                raise ValueError("Print generation does not match")
            existing = await connection.fetchrow(
                """
                SELECT id, attempt, printer_job_name, rendered_sha256,
                       rendered_byte_count
                FROM postal_local.print_attempts
                WHERE print_job_id = $1 AND station_event_identity = $2
                """,
                row["_id"],
                event_identity,
            )
            if existing is not None:
                if (existing["rendered_sha256"], existing["rendered_byte_count"]) != (
                    rendered_sha256,
                    rendered_byte_count,
                ):
                    raise ValueError(
                        "Idempotency key was used for different rendered output"
                    )
                return {
                    "id": str(existing["id"]),
                    "job_id": str(job_id),
                    "generation": row["generation"],
                    "attempt": existing["attempt"],
                    "job_name": existing["printer_job_name"],
                    "rendered_sha256": existing["rendered_sha256"],
                    "rendered_byte_count": existing["rendered_byte_count"],
                    "flow_id": str(row["flow_id"]),
                    "correlation_id": str(row["correlation_id"]),
                    "node_execution_id": str(row["node_execution_id"]),
                    "operation_id": str(row["operation_id"]),
                    "task_id": str(row["task_id"]),
                    "previous_state": row["state"],
                    "next_state": row["state"],
                    "duplicate": True,
                }
            if row["state"] in {"completed", "uncertain"}:
                raise ValueError(f"Print job is terminal: {row['state']}")
            attempt = await connection.fetchval(
                "SELECT COALESCE(max(attempt), 0) + 1 FROM postal_local.print_attempts WHERE print_job_id = $1",
                row["_id"],
            )
            model = PrintJob(
                str(row["id"]),
                str(row["run_id"]),
                row["sequence"],
                PrintJobKind(row["kind"]),
                row["generation"],
            )
            attempt_id = uuid5(job_id, f"attempt:{attempt}")
            name = printer_job_name(model, attempt)
            await connection.execute(
                """
                INSERT INTO postal_local.print_attempts
                    (id, print_job_id, attempt, printer_job_name, station_event_identity,
                     rendered_sha256, rendered_byte_count)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                attempt_id,
                row["_id"],
                attempt,
                name,
                event_identity,
                rendered_sha256,
                rendered_byte_count,
            )
            await connection.execute(
                "UPDATE postal_local.print_jobs SET state = 'submitting', updated_at = now() WHERE _id = $1",
                row["_id"],
            )
        return {
            "id": str(attempt_id),
            "job_id": str(job_id),
            "generation": row["generation"],
            "attempt": attempt,
            "job_name": name,
            "rendered_sha256": rendered_sha256,
            "rendered_byte_count": rendered_byte_count,
            "flow_id": str(row["flow_id"]),
            "correlation_id": str(row["correlation_id"]),
            "node_execution_id": str(row["node_execution_id"]),
            "operation_id": str(row["operation_id"]),
            "task_id": str(row["task_id"]),
            "previous_state": row["state"],
            "next_state": PrintJobState.SUBMITTING.value,
            "duplicate": False,
        }

    async def report_print_event(
        self,
        attempt_id: UUID,
        station_id: str,
        event: PrintAttemptEvent,
        printer_reference: str | None,
        error: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT attempt._id, attempt.state, attempt.print_job_id,
                       job.state job_state, job.id job_id, job.generation,
                       task.id task_id, tracked.id operation_id, execution.flow_id,
                       execution.correlation_id, execution.id node_execution_id
                FROM postal_local.print_attempts attempt
                JOIN postal_local.print_jobs job ON job._id = attempt.print_job_id
                JOIN postal_local.production_run_items item ON item._id = job.production_run_item_id
                JOIN postal_local.production_runs run ON run._id = item.production_run_id
                JOIN postal_local.production_tasks task ON task._id = item.production_task_id
                JOIN postal_local.operations local ON local._id = task.operation_id
                JOIN public.tracked_operations tracked ON tracked._id = local.tracked_operation_id
                JOIN public.node_executions execution ON execution._id = tracked.node_execution_id
                WHERE attempt.id = $1 AND run.station_id = $2 FOR UPDATE OF attempt, job
                """,
                attempt_id,
                station_id,
            )
            if row is None:
                raise LookupError("Print attempt not found")
            current_state = PrintJobState(row["job_state"])
            terminal_event_states = {
                PrintAttemptEvent.COMPLETED: PrintJobState.COMPLETED,
                PrintAttemptEvent.FAILED: PrintJobState.FAILED,
                PrintAttemptEvent.RESULT_UNKNOWN: PrintJobState.UNCERTAIN,
            }
            if (
                event in terminal_event_states
                and current_state is PrintJobState.SUBMITTING
            ):
                next_state = terminal_event_states[event]
            elif event is PrintAttemptEvent.ACCEPTED:
                next_state = reduce_print_state(current_state, event)
            elif current_state is terminal_event_states.get(event):
                return {
                    "flow_id": str(row["flow_id"]),
                    "correlation_id": str(row["correlation_id"]),
                    "node_execution_id": str(row["node_execution_id"]),
                    "operation_id": str(row["operation_id"]),
                    "task_id": str(row["task_id"]),
                    "job_id": str(row["job_id"]),
                    "generation": row["generation"],
                    "previous_state": current_state.value,
                    "next_state": current_state.value,
                    "duplicate": True,
                }
            else:
                next_state = reduce_print_state(current_state, event)
            completed = next_state in {
                PrintJobState.COMPLETED,
                PrintJobState.FAILED,
                PrintJobState.UNCERTAIN,
            }
            await connection.execute(
                "UPDATE postal_local.print_attempts SET state = $2, printer_reference = COALESCE($3, printer_reference), error = $4::jsonb, completed_at = CASE WHEN $5 THEN now() ELSE completed_at END, updated_at = now() WHERE _id = $1",
                row["_id"],
                next_state.value,
                printer_reference,
                orjson.dumps(dict(error)).decode() if error else None,
                completed,
            )
            await connection.execute(
                "UPDATE postal_local.print_jobs SET state = $2, updated_at = now() WHERE _id = $1",
                row["print_job_id"],
                next_state.value,
            )
            if next_state is PrintJobState.COMPLETED:
                await connection.execute(
                    """
                    UPDATE postal_local.production_tasks task
                    SET print_state = 'completed', physical_state = 'ready_for_processing', updated_at = now()
                    FROM postal_local.production_run_items item
                    WHERE item._id = (
                        SELECT production_run_item_id FROM postal_local.print_jobs WHERE _id = $1
                    ) AND task._id = item.production_task_id
                      AND NOT EXISTS (
                          SELECT 1 FROM postal_local.print_jobs sibling
                          WHERE sibling.production_run_item_id = item._id
                            AND sibling._id <> $1 AND sibling.state <> 'completed'
                      )
                    """,
                    row["print_job_id"],
                )
        return {
            "flow_id": str(row["flow_id"]),
            "correlation_id": str(row["correlation_id"]),
            "node_execution_id": str(row["node_execution_id"]),
            "operation_id": str(row["operation_id"]),
            "task_id": str(row["task_id"]),
            "job_id": str(row["job_id"]),
            "generation": row["generation"],
            "previous_state": current_state.value,
            "next_state": next_state.value,
            "duplicate": False,
        }

    async def record_carrier_observation(
        self,
        connection: Any,
        *,
        task_id: UUID,
        provider: str,
        external_event_id: str,
        provider_state: str,
        semantic_state: str | None,
        requires_reconciliation: bool,
        payload: Mapping[str, Any],
        observed_at: datetime | None,
    ) -> bool:
        inserted = await connection.fetchval(
            """
            INSERT INTO postal_local.carrier_observations
                (id, production_task_id, provider, external_event_id, provider_state,
                 semantic_state, requires_reconciliation, payload, observed_at)
            SELECT $1, task._id, $3, $4, $5, $6, $7, $8::jsonb, $9
            FROM postal_local.production_tasks task WHERE task.id = $2
            ON CONFLICT (production_task_id, provider, external_event_id) DO NOTHING
            RETURNING _id
            """,
            uuid.uuid4(),
            task_id,
            provider,
            external_event_id,
            provider_state,
            semantic_state,
            requires_reconciliation,
            orjson.dumps(dict(payload)).decode(),
            observed_at,
        )
        return inserted is not None

    async def upsert_carrier_proof(
        self,
        *,
        task_id: UUID,
        kind: str,
        state: str,
        provider_proof_id: str | None = None,
        artifact_id: UUID | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO postal_local.carrier_proofs
                    (id, production_task_id, kind, state, provider_proof_id, artifact_id, error)
                SELECT $1, task._id, $3, $4, $5, artifact._id, $7::jsonb
                FROM postal_local.production_tasks task
                LEFT JOIN postal_local.artifacts artifact ON artifact.id = $6
                WHERE task.id = $2
                ON CONFLICT (production_task_id, kind) DO UPDATE SET
                    state = EXCLUDED.state, provider_proof_id = EXCLUDED.provider_proof_id,
                    artifact_id = EXCLUDED.artifact_id, error = EXCLUDED.error, updated_at = now()
                """,
                uuid5(task_id, f"proof:{kind}"),
                task_id,
                kind,
                state,
                provider_proof_id,
                artifact_id,
                orjson.dumps(dict(error)).decode() if error else None,
            )
