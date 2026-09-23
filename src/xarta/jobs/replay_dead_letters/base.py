"""Periodically replay explicitly eligible dead-lettered requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nats
import orjson

from nats.js.api import AckPolicy
from nats.js.api import ConsumerConfig
from sanic import Blueprint

from xarta import env
from xarta.logging import logger
from xarta.nats.base import BaseNATSModel
from xarta.telemetry import nats_consumer_span
from xarta.telemetry import nats_producer_span

if TYPE_CHECKING:
    from typing import Any
    from typing import Final

    from sanic import Sanic

STREAM: Final[str] = "REQUESTS"
SUBJECT: Final[str] = "requests.dead-letter.>"
DURABLE: Final[str] = "dead-letter-replay"
BATCH_SIZE: Final[int] = env.extract(
    "DEAD_LETTER_REPLAY_BATCH_SIZE", default="100", dtype=int
)
MAX_ATTEMPTS: Final[int] = env.extract(
    "DEAD_LETTER_REPLAY_MAX_ATTEMPTS", default="3", dtype=int
)

bp = Blueprint(
    name="replay-dead-letters-v1",
    url_prefix="/job/v1/replay-dead-letters",
)

env.verify(blueprint=bp, required={"NATS_SERVERS"})


async def _ack(message: Any) -> None:
    await message.ack_sync()


async def replay_dead_letters(
    jetstream: Any,
    subscription: Any,
    *,
    batch_size: int,
    max_attempts: int,
) -> dict[str, int]:
    if batch_size < 1 or max_attempts < 1:
        raise ValueError("Dead-letter replay limits must be positive")
    counts = {"examined": 0, "replayed": 0, "ineligible": 0, "exhausted": 0}
    try:
        messages = await subscription.fetch(batch_size, timeout=1)
    except TimeoutError:
        return counts

    for message in messages:
        with nats_consumer_span(message):
            counts["examined"] += 1
            headers = message.headers or {}
            eligible = (
                headers.get("X-Xarta-Retryable") == "true"
                and headers.get("X-Xarta-Auto-Replay") == "true"
                and headers.get("X-Xarta-Failure-Class") == "retry_exhausted"
            )
            if not eligible:
                counts["ineligible"] += 1
                await _ack(message)
                continue

            try:
                replay_count = int(headers.get("X-Xarta-Periodic-Replay-Count", "0"))
            except ValueError:
                counts["ineligible"] += 1
                await _ack(message)
                continue

            configured_limit = headers.get("X-Xarta-Periodic-Replay-Limit")
            try:
                replay_limit = (
                    int(configured_limit)
                    if configured_limit is not None
                    else max_attempts
                )
            except ValueError:
                counts["ineligible"] += 1
                await _ack(message)
                continue
            if replay_count < 0 or replay_limit < 1:
                counts["ineligible"] += 1
                await _ack(message)
                continue
            was_final = headers.get("X-Xarta-Periodic-Retry-Final") == "true"
            if was_final != (replay_count >= replay_limit):
                counts["ineligible"] += 1
                await _ack(message)
                continue
            if replay_count >= replay_limit:
                counts["exhausted"] += 1
                await _ack(message)
                continue

            try:
                envelope = orjson.loads(message.data)
            except orjson.JSONDecodeError:
                counts["ineligible"] += 1
                await _ack(message)
                continue
            if not isinstance(envelope, dict):
                counts["ineligible"] += 1
                await _ack(message)
                continue
            stream_sequence = envelope.get("stream_sequence")
            if not isinstance(stream_sequence, int):
                counts["ineligible"] += 1
                await _ack(message)
                continue

            try:
                original = await jetstream.get_msg(STREAM, seq=stream_sequence)
            except nats.js.errors.NotFoundError:
                counts["ineligible"] += 1
                await logger.aerror(
                    "Dead-letter source message is no longer retained",
                    stream=STREAM,
                    stream_sequence=stream_sequence,
                )
                await _ack(message)
                continue

            next_count = replay_count + 1
            replay_headers = dict(original.headers or {})
            for key in list(replay_headers):
                if str(key).lower() in {"traceparent", "tracestate"}:
                    del replay_headers[key]
            original_message_id = replay_headers.get(
                "X-Xarta-Original-Message-Id", envelope.get("original_message_id")
            )
            if not isinstance(original_message_id, str) or not original_message_id:
                counts["ineligible"] += 1
                await _ack(message)
                continue
            replay_headers["Nats-Msg-Id"] = (
                f"{original_message_id}:periodic-replay:{next_count}"
            )
            replay_headers["X-Xarta-Original-Message-Id"] = original_message_id
            replay_headers["X-Xarta-Periodic-Replay-Count"] = str(next_count)
            replay_headers["X-Xarta-Periodic-Replay-Limit"] = str(replay_limit)
            replay_headers["X-Xarta-Periodic-Retry-Final"] = str(
                next_count >= replay_limit
            ).lower()
            with nats_producer_span(original.subject, replay_headers):
                await jetstream.publish(
                    original.subject,
                    original.data,
                    headers=replay_headers,
                )
            await _ack(message)
            counts["replayed"] += 1

    return counts


@bp.listener("after_server_start")
async def _launch(app: Sanic) -> None:
    client = await BaseNATSModel.open()
    try:
        jetstream = client.jetstream()
        config = ConsumerConfig(
            durable_name=DURABLE,
            ack_policy=AckPolicy.EXPLICIT,
            filter_subject=SUBJECT,
        )
        try:
            await jetstream.consumer_info(STREAM, DURABLE)
        except nats.js.errors.NotFoundError:
            subscription = await jetstream.pull_subscribe(
                stream=STREAM,
                subject=SUBJECT,
                durable=DURABLE,
                config=config,
            )
        else:
            subscription = await jetstream.pull_subscribe(
                stream=STREAM,
                subject=SUBJECT,
                durable=DURABLE,
            )
        counts = await replay_dead_letters(
            jetstream,
            subscription,
            batch_size=BATCH_SIZE,
            max_attempts=MAX_ATTEMPTS,
        )
        await logger.ainfo("Dead-letter replay completed", **counts)
    finally:
        await client.drain()
        app.stop()
