r"""NATS lifecycle and JetStream worker integration for Sanic."""

from __future__ import annotations

import asyncio
import contextlib
import datetime

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

import nats
import nats.js
import orjson

from nats.js.api import AckPolicy
from nats.js.api import ConsumerConfig

from xarta import env
from xarta.exceptions.protocol import PermanentError
from xarta.exceptions.protocol import TemporaryError
from xarta.exceptions.protocol import UnknownDAGNode
from xarta.execution import ExecutionMode
from xarta.logging import log_capability_lifecycle_change
from xarta.logging import logger
from xarta.logging import record_outcome_emitted
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import resolve_synchronous_outcomes
from xarta.telemetry import nats_consumer_span
from xarta.telemetry import nats_producer_span
from xarta.tracking import AttemptDisposition
from xarta.tracking import InMemoryTrackingStore
from xarta.tracking import NodeExecutionIdentityConflictError
from xarta.tracking import PostgresTrackingStore
from xarta.tracking import TrackingPostgresModel

from .base import BaseNATSModel

if TYPE_CHECKING:
    from sanic import Sanic


MessageHandler = Callable[..., Awaitable[Any]]
_DEAD_LETTER_HEADER_ALLOWLIST = frozenset(
    {
        "nats-msg-id",
        "flow-id",
        "correlation-id",
        "x-xarta-original-message-id",
        "x-xarta-periodic-replay-count",
        "x-xarta-periodic-replay-limit",
        "x-xarta-periodic-retry-final",
    }
)


class _DeadLetterPublished(Exception):
    """Terminal failure whose dead-letter record has already been published."""


def _periodic_retry_is_final(headers: dict | None) -> bool:
    values = headers or {}
    if values.get("X-Xarta-Periodic-Retry-Final") != "true":
        return False
    try:
        count = int(values["X-Xarta-Periodic-Replay-Count"])
        limit = int(values["X-Xarta-Periodic-Replay-Limit"])
    except (KeyError, TypeError, ValueError):
        return False
    return limit > 0 and count >= limit


def _tracked_execution_mode(_: NodeTask) -> ExecutionMode:
    return ExecutionMode.TRACKED


@dataclass(frozen=True)
class DeferredDelivery:
    """A live attempt owns execution; ask JetStream to redeliver after its lease."""

    delay: float


@dataclass
class NATSRuntime:
    """Resources owned by one NATS model in one Sanic application."""

    client: nats.aio.client.Client
    jetstream: nats.js.JetStreamContext
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    subscription: Any = None
    workers: set[asyncio.Task] = field(default_factory=set)
    expected_workers: int = 0
    heartbeat_interval: float = 10
    execution_attempt_lease_seconds: float = 120
    max_deliver: int = 20
    dead_letter_subject: str | None = None
    execution_mode_resolver: Callable[[NodeTask], ExecutionMode] = (
        _tracked_execution_mode
    )

    @property
    def ready(self) -> bool:
        return self.client.is_connected and len(self.workers) == self.expected_workers


def _runtime_key(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


class SanicNATSModel(BaseNATSModel):
    r"""
    A NATS model specifically curated for integrationg with Sanic. The
    class is responsible for maintaining the NATS and Jetstream client.
    """

    _runtime: NATSRuntime | None = None

    @classmethod
    def runtime(cls) -> NATSRuntime:
        if cls._runtime is None:
            raise RuntimeError(f"{cls.__name__} has not been registered")
        return cls._runtime

    @classmethod
    def jetstream(cls) -> nats.js.JetStreamContext:
        return cls.runtime().jetstream

    @classmethod
    async def _cleanup(cls, app: Sanic) -> None:
        runtimes = getattr(app.ctx, "nats_runtimes", {})
        runtime = runtimes.pop(_runtime_key(cls), None)
        if runtime is None:
            return

        runtime.stopping.set()
        shutdown_timeout = env.extract(
            "NATS_SHUTDOWN_TIMEOUT", default="30", dtype=float
        )

        if runtime.workers:
            _, pending = await asyncio.wait(runtime.workers, timeout=shutdown_timeout)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        drain_timeout = env.extract("NATS_DRAIN_TIMEOUT", default="5", dtype=float)
        cancelled = False
        try:
            await asyncio.wait_for(runtime.client.drain(), timeout=drain_timeout)
        except asyncio.CancelledError:
            cancelled = True
            await logger.awarning("NATS drain was cancelled; closing the connection")
        except Exception:
            await logger.awarning("NATS drain did not complete; closing the connection")
        finally:
            await asyncio.shield(runtime.client.close())
            if cls._runtime is runtime:
                cls._runtime = None
        if cancelled:
            raise asyncio.CancelledError

    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:
        r"""
        This method will serve as entrypoint to setup the necessary
        NATS and other connection details such as the database
        and name based on the specified environment variables.
        The model will be attached to a specific Sanic application.
        """
        if not hasattr(app.ctx, "nats_runtimes"):
            app.ctx.nats_runtimes = {}
        if not hasattr(app.ctx, "nats_runtime_locks"):
            app.ctx.nats_runtime_locks = {}

        key = _runtime_key(cls)
        lock = app.ctx.nats_runtime_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = app.ctx.nats_runtimes.get(key)
            if existing is not None:
                cls._runtime = existing
                return
            if cls._runtime is not None:
                raise RuntimeError(
                    f"{cls.__name__} is already registered with another application"
                )

            client = await cls.open(**kwargs)
            runtime = NATSRuntime(client=client, jetstream=client.jetstream())
            app.ctx.nats_runtimes[key] = runtime
            cls._runtime = runtime
            app.register_listener(cls._cleanup, "before_server_stop")


class SanicNATSConsumerModel(SanicNATSModel):
    r"""
    A consumer abstraction for allocating and maintaning
    NATS queue workers. In this worker we make the assumption
    that a message will be terminated when the exception
    is thrown at the worker level. Non-acknowledgements and
    retries sould be implemented in child implemetations.
    """

    @classmethod
    async def _publish(
        cls, task: NodeTask, headers: dict | None = None, **kwargs
    ) -> None:
        publish_headers = {
            key: value
            for key, value in (headers or {}).items()
            if not key.lower().startswith("x-xarta-")
        }
        publish_headers["flow-id"] = str(task.flow_id)
        publish_headers["correlation-id"] = str(task.correlation_id)
        publish_headers["Nats-Msg-Id"] = str(task.node_execution_id)
        subject = (
            f"requests.{task.flow_id}.tasks."
            f"{task.node_execution_id}.{task.node.kind}"
        )
        with nats_producer_span(subject, publish_headers):
            await cls.jetstream().publish(
                subject=subject,
                payload=orjson.dumps(task.dict(), default=str),
                headers=publish_headers,
            )

    @classmethod
    async def _heartbeat(cls, msg, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await msg.in_progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                await logger.aexception("Failed to send a NATS progress heartbeat")
                return

    @classmethod
    async def _run_handler(cls, msg, fn: MessageHandler, **kwargs) -> Any:
        interval = kwargs.pop("heartbeat_interval", 10)
        heartbeat = asyncio.create_task(cls._heartbeat(msg, interval))
        with nats_consumer_span(msg):
            handler = asyncio.create_task(fn(msg=msg, **kwargs))
            try:
                return await handler
            finally:
                if not handler.done():
                    handler.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await handler
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    @classmethod
    async def _publish_dead_letter(
        cls,
        runtime: NATSRuntime,
        msg,
        error: Exception,
        *,
        classification: str,
        error_code: str,
        retryable: bool,
        outcome: OutcomeEmission | None = None,
    ) -> None:
        headers = {
            key: value
            for key, value in (msg.headers or {}).items()
            if key.lower() in _DEAD_LETTER_HEADER_ALLOWLIST
        }
        sequence = getattr(getattr(msg, "metadata", None), "sequence", None)
        stream_sequence = getattr(sequence, "stream", "unknown")
        original_message_id = headers.get(
            "Nats-Msg-Id", f"{msg.subject}:{stream_sequence}"
        )
        headers["Nats-Msg-Id"] = f"{original_message_id}:dead-letter"
        delivery_count = getattr(getattr(msg, "metadata", None), "num_delivered", 1)
        headers.update(
            {
                "X-Xarta-Dead-Letter-Version": "1",
                "X-Xarta-Failure-Class": classification,
                "X-Xarta-Error-Code": error_code,
                "X-Xarta-Retryable": str(retryable).lower(),
                "X-Xarta-Auto-Replay": str(
                    isinstance(error, TemporaryError) and error.auto_replay
                ).lower(),
                "X-Xarta-Delivery-Count": str(delivery_count),
            }
        )
        try:
            original_payload = orjson.loads(msg.data)
        except orjson.JSONDecodeError:
            original_payload = {}
        node = (
            original_payload.get("node", {})
            if isinstance(original_payload, dict)
            else {}
        )
        metadata = getattr(msg, "metadata", None)
        first_seen_at = getattr(metadata, "timestamp", None)
        payload = {
            "schema_version": 1,
            "classification": classification,
            "error_code": error_code,
            "error": (
                "Retryable NATS delivery exhausted"
                if retryable
                else "Permanent NATS delivery failure"
            ),
            "original_subject": msg.subject,
            "original_message_id": original_message_id,
            "stream_sequence": stream_sequence,
            "consumer": (
                runtime.dead_letter_subject.rsplit(".", 1)[-1]
                if runtime.dead_letter_subject
                else None
            ),
            "delivery_count": delivery_count,
            "first_seen_at": first_seen_at,
            "failed_at": datetime.datetime.now(datetime.UTC),
            "flow_id": (
                original_payload.get("flow_id")
                if isinstance(original_payload, dict)
                else None
            ),
            "node_id": node.get("id") if isinstance(node, dict) else None,
            "node_execution_id": (
                original_payload.get("node_execution_id")
                if isinstance(original_payload, dict)
                else None
            ),
        }
        if outcome is not None:
            payload["outcome"] = {
                "outcome": outcome.outcome,
                "details": outcome.details,
            }
        if runtime.dead_letter_subject is None:
            raise RuntimeError("Dead-letter subject is not configured")

        attempt = 0
        while True:
            try:
                with nats_producer_span(runtime.dead_letter_subject, headers):
                    await runtime.jetstream.publish(
                        runtime.dead_letter_subject,
                        orjson.dumps(payload, default=str),
                        headers=headers,
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                await logger.awarning(
                    "Dead-letter publication failed; retaining message ownership",
                    subject=runtime.dead_letter_subject,
                    retry_in=min(attempt, 5),
                )
                await msg.in_progress()
                await asyncio.sleep(min(attempt, 5))

    @classmethod
    async def _handle_exhausted_retry(
        cls, runtime: NATSRuntime, msg, error: TemporaryError
    ) -> None:
        await cls._publish_dead_letter(
            runtime,
            msg,
            error,
            classification=error.classification,
            error_code=error.error_code,
            retryable=True,
            outcome=error.outcome,
        )

    @classmethod
    async def _handle_permanent_failure(
        cls, runtime: NATSRuntime, msg, error: PermanentError
    ) -> None:
        await cls._publish_dead_letter(
            runtime,
            msg,
            error,
            classification=error.classification,
            error_code=error.error_code,
            retryable=False,
            outcome=error.outcome,
        )

    @classmethod
    async def _worker(cls, runtime: NATSRuntime, fn: MessageHandler, **kwargs) -> None:
        while not runtime.stopping.is_set():
            try:
                messages = await runtime.subscription.fetch(1, timeout=1)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                await logger.aexception("Failed to fetch a NATS message")
                await asyncio.sleep(1)
                continue

            for msg in messages:
                try:
                    handler_result = await cls._run_handler(msg, fn, **kwargs)
                except _DeadLetterPublished:
                    try:
                        await msg.term()
                    except Exception:
                        await logger.aexception("Failed to terminate a NATS message")
                except TemporaryError as ex:
                    delivery = getattr(
                        getattr(msg, "metadata", None), "num_delivered", 1
                    )
                    max_deliver = min(
                        runtime.max_deliver,
                        ex.max_deliver or runtime.max_deliver,
                    )
                    if delivery >= max_deliver:
                        try:
                            await cls._handle_exhausted_retry(runtime, msg, ex)
                            await msg.term()
                        except Exception:
                            await logger.aexception(
                                "Failed to dead-letter an exhausted NATS message"
                            )
                            with contextlib.suppress(Exception):
                                await msg.nak(delay=1)
                        continue
                    await logger.awarning(
                        "NATS handler failed temporarily",
                        error=str(ex),
                        retry_in=ex.retry_delay(delivery),
                    )
                    try:
                        await msg.nak(delay=ex.retry_delay(delivery))
                    except Exception:
                        await logger.aexception("Failed to NAK a NATS message")
                except PermanentError as ex:
                    try:
                        await cls._handle_permanent_failure(runtime, msg, ex)
                        await msg.term()
                    except Exception:
                        await logger.aexception(
                            "Failed to dead-letter a permanent NATS message"
                        )
                        with contextlib.suppress(Exception):
                            await msg.nak(delay=1)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await msg.nak()
                    raise
                except Exception:
                    await logger.aexception("NATS handler failed unexpectedly")
                    unexpected = TemporaryError(
                        "Unexpected NATS handler failure",
                        delay=1,
                        max_deliver=3,
                        classification="unexpected_retry_exhausted",
                        error_code="unexpected_handler_error",
                        backoff_multiplier=2,
                    )
                    delivery = getattr(
                        getattr(msg, "metadata", None), "num_delivered", 1
                    )
                    if delivery >= min(runtime.max_deliver, 3):
                        try:
                            await cls._handle_exhausted_retry(runtime, msg, unexpected)
                            await msg.term()
                        except Exception:
                            await logger.aexception(
                                "Failed to dead-letter an unexpected NATS failure"
                            )
                            with contextlib.suppress(Exception):
                                await msg.nak(delay=1)
                    else:
                        with contextlib.suppress(Exception):
                            await msg.nak(delay=1)
                else:
                    if isinstance(handler_result, DeferredDelivery):
                        try:
                            await msg.nak(delay=handler_result.delay)
                        except Exception:
                            await logger.aexception(
                                "Failed to defer an actively owned NATS message"
                            )
                        continue
                    try:
                        await msg.ack()
                    except Exception:
                        # The broker will redeliver when the ACK was not persisted.
                        await logger.aexception("Failed to ACK a NATS message")

    @classmethod
    async def register(
        cls,
        app: Sanic,
        stream: str,
        subject: str,
        fn: MessageHandler,
        workers: int = 1,
        **kwargs,
    ) -> None:
        if not hasattr(app.ctx, "nats_consumer_locks"):
            app.ctx.nats_consumer_locks = {}
        lock = app.ctx.nats_consumer_locks.setdefault(_runtime_key(cls), asyncio.Lock())
        async with lock:
            await cls._register_consumer(
                app=app,
                stream=stream,
                subject=subject,
                fn=fn,
                workers=workers,
                **kwargs,
            )

    @classmethod
    async def _register_consumer(
        cls,
        app: Sanic,
        stream: str,
        subject: str,
        fn: MessageHandler,
        workers: int,
        **kwargs,
    ) -> None:
        await super().register(app=app)
        runtime = cls.runtime()
        if runtime.subscription is not None:
            return

        ack_wait = env.extract("NATS_ACK_WAIT", default="120", dtype=float)
        max_deliver = env.extract("NATS_MAX_DELIVER", default="20", dtype=int)
        heartbeat_interval = env.extract(
            "NATS_HEARTBEAT_INTERVAL", default="10", dtype=float
        )
        attempt_lease_seconds = env.extract(
            "EXECUTION_ATTEMPT_LEASE_SECONDS", default=str(ack_wait), dtype=float
        )
        num_nats_workers = env.extract(
            "NATS_WORKERS", optional=True, default=str(workers), dtype=int
        )
        if ack_wait <= 0:
            raise ValueError("NATS_ACK_WAIT must be greater than zero")
        if max_deliver < 1:
            raise ValueError("NATS_MAX_DELIVER must be at least one")
        if heartbeat_interval <= 0 or heartbeat_interval > ack_wait / 2:
            raise ValueError(
                "NATS_HEARTBEAT_INTERVAL must be positive and no more than half NATS_ACK_WAIT"
            )
        if attempt_lease_seconds < heartbeat_interval * 2:
            raise ValueError(
                "EXECUTION_ATTEMPT_LEASE_SECONDS must be at least twice "
                "NATS_HEARTBEAT_INTERVAL"
            )
        if num_nats_workers < 1 or num_nats_workers > 100:
            raise ValueError("NATS_WORKERS must be between 1 and 100")

        durable = kwargs.pop("durable", None)
        if not durable:
            raise ValueError("A durable consumer name is required")
        config = ConsumerConfig(
            durable_name=durable,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ack_wait,
            # Keep one broker delivery in reserve if dead-letter publication fails.
            max_deliver=max_deliver + 1,
            filter_subject=subject,
        )
        try:
            await runtime.jetstream.consumer_info(stream, durable)
        except nats.js.errors.NotFoundError:
            runtime.subscription = await runtime.jetstream.pull_subscribe(
                stream=stream,
                subject=subject,
                durable=durable,
                config=config,
                **kwargs,
            )
        else:
            await runtime.jetstream.add_consumer(stream, config)
            runtime.subscription = await runtime.jetstream.pull_subscribe(
                stream=stream,
                subject=subject,
                durable=durable,
                **kwargs,
            )

        runtime.expected_workers = num_nats_workers
        runtime.heartbeat_interval = heartbeat_interval
        runtime.execution_attempt_lease_seconds = attempt_lease_seconds
        runtime.max_deliver = max_deliver
        runtime.dead_letter_subject = (
            f"{stream.lower()}.dead-letter.{durable or cls.__name__.lower()}"
        )

        for index in range(num_nats_workers):
            task = asyncio.create_task(
                cls._worker(
                    runtime=runtime,
                    app=app,
                    fn=fn,
                    heartbeat_interval=heartbeat_interval,
                ),
                name=f"{cls.__name__}-{index}",
            )
            runtime.workers.add(task)
            task.add_done_callback(runtime.workers.discard)


class SanicNATSJobsConsumerModel(SanicNATSConsumerModel):
    r"""
    A consumer model specifically made for jobs.
    """

    @classmethod
    async def register(
        cls, app: Sanic, name: str, fn: MessageHandler, workers: int = 1, **kwargs
    ) -> None:
        await super().register(
            app=app,
            stream="JOBS",
            subject=f"jobs.{name}.*",
            durable=name,
            fn=fn,
            workers=workers,
            **kwargs,
        )


class SanicNATSSynchronousRequestsConsumerModel(SanicNATSConsumerModel):
    """Execute final-before-return DAG nodes without PostgreSQL tracking."""

    @classmethod
    async def register(
        cls, app: Sanic, name: str, fn: MessageHandler, workers: int = 1, **kwargs
    ) -> None:
        await super().register(
            app=app,
            stream="REQUESTS",
            subject=f"requests.*.tasks.*.{name}",
            durable=name,
            fn=fn,
            workers=workers,
            **kwargs,
        )

    @classmethod
    async def _publish_outcomes(
        cls,
        task: NodeTask,
        outcomes: tuple[OutcomeEmission, ...],
        source: str,
        headers: dict,
        log,
    ) -> None:
        events, successors = resolve_synchronous_outcomes(task, outcomes, source)
        for event in events:
            record_outcome_emitted(event)
            await log.ainfo(
                task.node.kind,
                outcome_event_id=str(event.id),
                outcome=event.outcome,
                subject_kind=event.subject.kind if event.subject else None,
            )
        await asyncio.gather(
            *(cls._publish(successor, headers=headers) for successor in successors)
        )

    @classmethod
    async def _handle_exhausted_retry(
        cls, runtime: NATSRuntime, msg, error: TemporaryError
    ) -> None:
        await super()._handle_exhausted_retry(runtime, msg, error)
        if error.auto_replay and not _periodic_retry_is_final(msg.headers):
            return
        headers = dict(msg.headers or {})
        task = NodeTask.fromdict(orjson.loads(msg.data))
        outcome = error.outcome or OutcomeEmission(CommonOutcome.RETRY_EXHAUSTED)
        log = logger.bind(
            flow_id=str(task.flow_id),
            correlation_id=str(task.correlation_id or task.flow_id),
            node_id=str(task.node.id),
            node_execution_id=str(task.node_execution_id),
            kind=task.node.kind,
        )
        await cls._publish_outcomes(task, (outcome,), "retry", headers, log)
        await log_capability_lifecycle_change(
            log,
            flow_id=task.flow_id,
            correlation_id=task.correlation_id or task.flow_id,
            node_id=task.node.id,
            node_execution_id=task.node_execution_id,
            capability=task.node.kind,
            execution_mode=ExecutionMode.SYNCHRONOUS.value,
            lifecycle="node_execution",
            action="retry_exhausted",
            previous_state="executing",
            next_state="failed",
            source="retry",
        )

    @classmethod
    async def _handle_synchronous_request(
        cls, runtime: NATSRuntime, msg, fn, handler_kwargs
    ):
        try:
            headers = dict(msg.headers or {})
            task = NodeTask.fromdict(orjson.loads(msg.data))
        except (
            KeyError,
            TypeError,
            ValueError,
            UnknownDAGNode,
            orjson.JSONDecodeError,
        ) as ex:
            await logger.aexception("Discarding malformed NATS request")
            raise PermanentError(
                "NATS request payload is malformed",
                classification="malformed",
                error_code="malformed_request",
            ) from ex

        node = task.node
        log = logger.bind(
            flow_id=str(task.flow_id),
            correlation_id=str(task.correlation_id or task.flow_id),
            node_id=str(node.id),
            node_execution_id=str(task.node_execution_id),
            kind=node.kind,
        )
        permanent_error = None
        try:
            await log.ainfo(node.kind, start=True)
            await log_capability_lifecycle_change(
                log,
                flow_id=task.flow_id,
                correlation_id=task.correlation_id or task.flow_id,
                node_id=node.id,
                node_execution_id=task.node_execution_id,
                capability=node.kind,
                execution_mode=ExecutionMode.SYNCHRONOUS.value,
                lifecycle="node_execution",
                action="started",
                previous_state="scheduled",
                next_state="executing",
            )
            result = await fn(
                msg=msg,
                node=node,
                flow_id=task.flow_id,
                headers=headers,
                logger=log,
                task=task,
                **handler_kwargs,
            )
            if isinstance(result, OutcomeEmission):
                result = CapabilityResult((result,))
            if not isinstance(result, CapabilityResult):
                raise TypeError(
                    f"Handler for {node.kind} returned unsupported result "
                    f"{type(result).__name__}"
                )
            if result.waiting_feedback or result.outcomes_persisted:
                raise TypeError(
                    f"Synchronous handler for {node.kind} returned a tracked result"
                )
        except TemporaryError:
            raise
        except PermanentError as ex:
            await log.aexception("Request handler failed permanently")
            permanent_error = ex
            if ex.outcome is None:
                ex.outcome = OutcomeEmission(CommonOutcome.EXECUTION_FAILED)
            await SanicNATSConsumerModel._handle_permanent_failure.__func__(
                cls, runtime, msg, ex
            )
            result = CapabilityResult((ex.outcome,))
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            await log.aexception("Request handler failed")
            raise TemporaryError(
                "Unexpected request handler failure",
                delay=1,
                outcome=OutcomeEmission(CommonOutcome.EXECUTION_FAILED),
                max_deliver=3,
                classification="unexpected_retry_exhausted",
                error_code="unexpected_handler_error",
                backoff_multiplier=2,
            ) from ex

        try:
            await cls._publish_outcomes(task, result.outcomes, node.kind, headers, log)
        except Exception as ex:
            if permanent_error is not None:
                raise TemporaryError(
                    "Could not publish permanent failure outcomes",
                    delay=1,
                    outcome=permanent_error.outcome,
                    classification=permanent_error.classification,
                    error_code=permanent_error.error_code,
                ) from ex
            raise TemporaryError(
                "Could not publish synchronous outcomes", delay=1
            ) from ex
        await log.ainfo(node.kind, complete=True)
        await log_capability_lifecycle_change(
            log,
            flow_id=task.flow_id,
            correlation_id=task.correlation_id or task.flow_id,
            node_id=node.id,
            node_execution_id=task.node_execution_id,
            capability=node.kind,
            execution_mode=ExecutionMode.SYNCHRONOUS.value,
            lifecycle="node_execution",
            action="completed",
            previous_state="executing",
            next_state="failed" if permanent_error is not None else "resolved",
        )
        if permanent_error is not None:
            raise _DeadLetterPublished from permanent_error

    @classmethod
    async def _worker(cls, runtime: NATSRuntime, fn: MessageHandler, **kwargs) -> None:
        async def handle_request(msg, **handler_kwargs):
            return await cls._handle_synchronous_request(
                runtime, msg, fn, handler_kwargs
            )

        await super()._worker(runtime=runtime, fn=handle_request, **kwargs)


class SanicNATSRequestsConsumerModel(SanicNATSSynchronousRequestsConsumerModel):
    r"""
    A consumer model specifically made for requests.
    """

    _tracking_store = InMemoryTrackingStore()

    @classmethod
    def tracking_store(cls):
        return cls._tracking_store

    @classmethod
    async def publish(cls, tasks: list[NodeTask], headers: dict | None = None) -> None:
        if not tasks:
            return

        await asyncio.gather(
            *[cls._publish(task=task, headers=headers) for task in tasks]
        )

    @classmethod
    async def register(
        cls,
        app: Sanic,
        name: str,
        fn: MessageHandler,
        execution_mode_resolver: Callable[[NodeTask], ExecutionMode],
        workers: int = 1,
        **kwargs,
    ) -> None:
        await TrackingPostgresModel.register(app)
        cls._tracking_store = PostgresTrackingStore(TrackingPostgresModel.pool())
        await super().register(
            app=app,
            name=name,
            fn=fn,
            workers=workers,
            **kwargs,
        )
        runtime = cls.runtime()
        if runtime.execution_mode_resolver is _tracked_execution_mode:
            runtime.execution_mode_resolver = execution_mode_resolver

    @staticmethod
    def _execution_mode(runtime: NATSRuntime, task: NodeTask) -> ExecutionMode:
        mode = runtime.execution_mode_resolver(task)
        if not isinstance(mode, ExecutionMode):
            raise TypeError("Execution mode resolver returned an invalid mode")
        return mode

    @classmethod
    async def _handle_exhausted_retry(
        cls, runtime: NATSRuntime, msg, error: TemporaryError
    ) -> None:
        headers = dict(msg.headers or {})
        task = NodeTask.fromdict(orjson.loads(msg.data))
        try:
            mode = cls._execution_mode(runtime, task)
        except Exception:
            mode = ExecutionMode.SYNCHRONOUS
            error = TemporaryError(
                "Could not resolve capability execution mode",
                outcome=OutcomeEmission(CommonOutcome.EXECUTION_FAILED),
            )
        if mode is ExecutionMode.SYNCHRONOUS:
            await SanicNATSSynchronousRequestsConsumerModel._handle_exhausted_retry.__func__(
                cls, runtime, msg, error
            )
            return
        outcome = error.outcome or OutcomeEmission(CommonOutcome.RETRY_EXHAUSTED)
        await SanicNATSConsumerModel._handle_exhausted_retry.__func__(
            cls, runtime, msg, error
        )
        if error.auto_replay and not _periodic_retry_is_final(msg.headers):
            return
        await logger.ainfo(
            "Request retry exhausted",
            flow_id=str(task.flow_id),
            correlation_id=str(task.correlation_id or task.flow_id),
            node_id=str(task.node.id),
            node_execution_id=str(task.node_execution_id),
            kind=task.node.kind,
            outcome=outcome.outcome,
        )
        applied = await cls.tracking_store().apply_execution_outcomes(
            task, (outcome,), source="retry"
        )
        for event in applied.outcomes:
            record_outcome_emitted(event)
        await cls.publish(list(applied.successor_tasks), headers=headers)
        await log_capability_lifecycle_change(
            logger,
            flow_id=task.flow_id,
            correlation_id=task.correlation_id or task.flow_id,
            node_id=task.node.id,
            node_execution_id=task.node_execution_id,
            capability=task.node.kind,
            execution_mode=ExecutionMode.TRACKED.value,
            lifecycle="node_execution",
            action="retry_exhausted",
            previous_state="executing",
            next_state="failed",
            source="retry",
        )

    @classmethod
    async def _worker(cls, runtime: NATSRuntime, fn: MessageHandler, **kwargs) -> None:
        async def handle_request(msg, **handler_kwargs):
            lease_seconds = handler_kwargs.pop(
                "execution_attempt_lease_seconds",
                runtime.execution_attempt_lease_seconds,
            )
            renewal_interval = handler_kwargs.pop(
                "attempt_lease_renewal_interval", runtime.heartbeat_interval
            )
            try:
                headers = dict(msg.headers or {})
                task = NodeTask.fromdict(orjson.loads(msg.data))
            except (
                KeyError,
                TypeError,
                ValueError,
                UnknownDAGNode,
                orjson.JSONDecodeError,
            ) as ex:
                await logger.aexception("Discarding malformed NATS request")
                raise PermanentError(
                    "NATS request payload is malformed",
                    classification="malformed",
                    error_code="malformed_request",
                ) from ex

            flow_id = task.flow_id
            node = task.node
            try:
                mode = cls._execution_mode(runtime, task)
            except Exception as ex:
                await logger.aexception("Could not resolve capability execution mode")
                permanent = PermanentError(
                    "Could not resolve capability execution mode",
                    classification="permanent",
                    error_code="execution_mode_resolution_failed",
                    outcome=OutcomeEmission(CommonOutcome.EXECUTION_FAILED),
                )
                await SanicNATSConsumerModel._handle_permanent_failure.__func__(
                    cls, runtime, msg, permanent
                )
                try:
                    await cls._publish_outcomes(
                        task,
                        (permanent.outcome,),
                        "admission",
                        headers,
                        logger,
                    )
                except Exception as publish_ex:
                    raise TemporaryError(
                        "Could not publish admission failure",
                        delay=1,
                        outcome=permanent.outcome,
                        classification=permanent.classification,
                        error_code=permanent.error_code,
                    ) from publish_ex
                raise _DeadLetterPublished from ex
            if mode is ExecutionMode.SYNCHRONOUS:
                return await cls._handle_synchronous_request(
                    runtime, msg, fn, handler_kwargs
                )
            log = logger.bind(
                flow_id=str(flow_id),
                correlation_id=str(task.correlation_id or flow_id),
                node_id=str(node.id),
                node_execution_id=str(task.node_execution_id),
                kind=node.kind,
            )
            try:
                admission = await cls.tracking_store().begin_attempt(
                    task, lease_seconds=lease_seconds
                )
            except NodeExecutionIdentityConflictError as ex:
                # This is a poison delivery, not a transient persistence failure.
                # The existing execution identity must remain untouched.
                raise PermanentError(
                    "Node execution identity conflicts with the accepted request",
                    classification="poison",
                    error_code="node_execution_identity_conflict",
                ) from ex
            except Exception as ex:
                raise TemporaryError(
                    "Could not begin durable node execution", delay=1
                ) from ex
            if admission.disposition is AttemptDisposition.ACTIVE_ATTEMPT:
                if admission.lease_until is None:
                    raise RuntimeError(
                        "Active attempt admission did not include lease_until"
                    )
                delay = max(
                    0.1,
                    (
                        admission.lease_until - datetime.datetime.now(datetime.UTC)
                    ).total_seconds(),
                )
                await log.ainfo(
                    node.kind,
                    duplicate=True,
                    disposition=admission.disposition.value,
                    retry_in=delay,
                )
                return DeferredDelivery(delay)
            if admission.disposition is not AttemptDisposition.EXECUTE:
                if admission.disposition in {
                    AttemptDisposition.WAITING_FEEDBACK,
                    AttemptDisposition.DUPLICATE_TERMINAL,
                }:
                    successors = await cls.tracking_store().replay_successors(task)
                    try:
                        await cls.publish(list(successors), headers=headers)
                    except Exception as ex:
                        raise TemporaryError(
                            "Could not republish successors", delay=1
                        ) from ex
                await log.ainfo(
                    node.kind,
                    duplicate=True,
                    disposition=admission.disposition.value,
                )
                return
            attempt = admission.attempt
            if attempt is None:
                raise RuntimeError("Executable admission did not include an attempt")
            renewal = asyncio.create_task(
                cls._renew_attempt_lease(attempt, lease_seconds, renewal_interval),
                name=f"execution-attempt-lease-{attempt.id}",
            )
            owner = asyncio.current_task()
            if owner is not None:
                owner.add_done_callback(lambda _task: renewal.cancel())
            execution_error = None
            permanent_error = None
            try:
                await log.ainfo(node.kind, start=True)
                await log_capability_lifecycle_change(
                    log,
                    flow_id=task.flow_id,
                    correlation_id=task.correlation_id or task.flow_id,
                    node_id=node.id,
                    node_execution_id=task.node_execution_id,
                    capability=node.kind,
                    execution_mode=ExecutionMode.TRACKED.value,
                    lifecycle="node_execution",
                    action="attempt_started",
                    previous_state=(
                        "scheduled" if attempt.number == 1 else "executing"
                    ),
                    next_state="executing",
                )
                handler_result = await fn(
                    msg=msg,
                    node=node,
                    flow_id=flow_id,
                    headers=headers,
                    logger=log,
                    task=task,
                    **handler_kwargs,
                )
                if isinstance(handler_result, OutcomeEmission):
                    handler_result = CapabilityResult((handler_result,))
                if not isinstance(handler_result, CapabilityResult):
                    raise TypeError(
                        f"Handler for {node.kind} returned unsupported result "
                        f"{type(handler_result).__name__}"
                    )
                if renewal.done() and not renewal.result():
                    raise TemporaryError("Execution attempt lease was lost", delay=1)
            except TemporaryError as ex:
                if ex.outcome is not None:
                    delivery = getattr(
                        getattr(msg, "metadata", None), "num_delivered", 1
                    )
                    await log.awarning(
                        "Request handler failed temporarily",
                        outcome=ex.outcome.outcome,
                        retry_in=ex.retry_delay(delivery),
                    )
                with contextlib.suppress(Exception):
                    await cls.tracking_store().complete_attempt(
                        attempt, {"error": str(ex), "retryable": True}
                    )
                renewal.cancel()
                raise
            except PermanentError as ex:
                await log.aexception("Request handler failed permanently")
                permanent_error = ex
                execution_error = {"error": str(ex), "retryable": False}
                if ex.outcome is None:
                    ex.outcome = OutcomeEmission(CommonOutcome.EXECUTION_FAILED)
                await SanicNATSConsumerModel._handle_permanent_failure.__func__(
                    cls, runtime, msg, ex
                )
                handler_result = CapabilityResult((ex.outcome,))
            except asyncio.CancelledError:
                renewal.cancel()
                raise
            except Exception as ex:
                await log.aexception("Request handler failed")
                with contextlib.suppress(Exception):
                    await cls.tracking_store().complete_attempt(
                        attempt, {"error": str(ex), "retryable": True}
                    )
                renewal.cancel()
                raise TemporaryError(
                    "Unexpected request handler failure",
                    delay=1,
                    outcome=OutcomeEmission(CommonOutcome.EXECUTION_FAILED),
                    max_deliver=3,
                    classification="unexpected_retry_exhausted",
                    error_code="unexpected_handler_error",
                ) from ex

            try:
                if handler_result.waiting_feedback:
                    completed = await cls.tracking_store().complete_attempt(
                        attempt, execution_error
                    )
                    renewal.cancel()
                    if not completed:
                        raise TemporaryError(
                            "Execution attempt lease was lost", delay=1
                        )
                    await log.ainfo(node.kind, waiting_feedback=True)
                    await log_capability_lifecycle_change(
                        log,
                        flow_id=task.flow_id,
                        correlation_id=task.correlation_id or task.flow_id,
                        node_id=node.id,
                        node_execution_id=task.node_execution_id,
                        capability=node.kind,
                        execution_mode=ExecutionMode.TRACKED.value,
                        lifecycle="node_execution",
                        action="waiting_for_feedback",
                        previous_state="executing",
                        next_state="waiting_feedback",
                    )
                    return
                if handler_result.outcomes_persisted:
                    completed = await cls.tracking_store().complete_attempt(
                        attempt, execution_error
                    )
                    renewal.cancel()
                    if not completed:
                        raise TemporaryError(
                            "Execution attempt lease was lost", delay=1
                        )
                    await log_capability_lifecycle_change(
                        log,
                        flow_id=task.flow_id,
                        correlation_id=task.correlation_id or task.flow_id,
                        node_id=node.id,
                        node_execution_id=task.node_execution_id,
                        capability=node.kind,
                        execution_mode=ExecutionMode.TRACKED.value,
                        lifecycle="node_execution",
                        action="completed",
                        previous_state="executing",
                        next_state=(
                            "failed" if permanent_error is not None else "resolved"
                        ),
                    )
                    return
                applied = await cls.tracking_store().apply_execution_outcomes(
                    task, handler_result.outcomes, source=node.kind
                )
                for outcome in applied.outcomes:
                    record_outcome_emitted(outcome)
                    await log.ainfo(
                        node.kind,
                        outcome_event_id=str(outcome.id),
                        outcome=outcome.outcome,
                        subject_kind=(
                            outcome.subject.kind if outcome.subject else None
                        ),
                    )
                await cls.publish(list(applied.successor_tasks), headers=headers)
                completed = await cls.tracking_store().complete_attempt(
                    attempt, execution_error
                )
                if not completed:
                    raise TemporaryError("Execution attempt lease was lost", delay=1)
                renewal.cancel()
            except Exception as ex:
                with contextlib.suppress(Exception):
                    await cls.tracking_store().complete_attempt(
                        attempt, {"error": str(ex), "retryable": True}
                    )
                renewal.cancel()
                if permanent_error is not None:
                    raise TemporaryError(
                        "Could not persist or publish permanent failure outcomes",
                        delay=1,
                        outcome=permanent_error.outcome,
                        classification=permanent_error.classification,
                        error_code=permanent_error.error_code,
                    ) from ex
                raise TemporaryError(
                    "Could not persist or publish outcomes",
                    delay=1,
                ) from ex
            await log.ainfo(node.kind, complete=True)
            await log_capability_lifecycle_change(
                log,
                flow_id=task.flow_id,
                correlation_id=task.correlation_id or task.flow_id,
                node_id=node.id,
                node_execution_id=task.node_execution_id,
                capability=node.kind,
                execution_mode=ExecutionMode.TRACKED.value,
                lifecycle="node_execution",
                action="completed",
                previous_state="executing",
                next_state="failed" if permanent_error is not None else "resolved",
            )
            if permanent_error is not None:
                raise _DeadLetterPublished from permanent_error

        await SanicNATSConsumerModel._worker.__func__(
            cls,
            runtime=runtime,
            fn=handle_request,
            execution_attempt_lease_seconds=runtime.execution_attempt_lease_seconds,
            attempt_lease_renewal_interval=runtime.heartbeat_interval,
            **kwargs,
        )

    @classmethod
    async def _renew_attempt_lease(
        cls, attempt, lease_seconds: float, interval: float
    ) -> bool:
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await cls.tracking_store().renew_attempt_lease(
                    attempt, lease_seconds=lease_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await logger.aexception("Failed to renew execution attempt lease")
                return False
            if not renewed:
                await logger.awarning(
                    "Execution attempt lease ownership was lost",
                    attempt_id=str(attempt.id),
                )
                return False
