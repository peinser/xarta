r"""
Blueprint definition of a job that is responsible for configuring NATS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nats import js
from sanic import Blueprint

from xarta import env
from xarta.nats import constants

from .nats import SetupNATSModel as NATS

if TYPE_CHECKING:
    from sanic import Sanic


bp = Blueprint(
    name="setup-nats",
    url_prefix="/job/v1/setup-nats",
)


env.verify(
    blueprint=bp,
    required={"NATS_SERVERS"},
)


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    await NATS.register(app)


@bp.listener("after_server_start")
async def _launch(app: Sanic) -> None:
    # Retrieve the NATS stream configurations.
    stream_configurations = (
        _jobs_stream_config(),
        _requests_stream_config(),
        _documents_stream_config(),
        _archive_commands_stream_config(),
    )

    # Update or create the provided streams.
    for stream_config in stream_configurations:
        try:
            await NATS.js().stream_info(stream_config.name)
        except js.errors.NotFoundError:
            await NATS.js().add_stream(stream_config)
        else:
            await NATS.js().update_stream(stream_config)

    app.stop()


def _requests_stream_config() -> js.api.StreamConfig:
    return js.api.StreamConfig(
        name=constants.STREAM_REQUESTS,
        description="Events related to Xarta async document requests.",
        subjects=["requests.>"],
        retention="limits",
        max_consumers=-1,
        max_msgs=10_000_000,
        max_bytes=10 * 1024 * 1024 * 1024,
        discard="old",
        max_age=30 * 24 * 60 * 60,
        max_msgs_per_subject=-1,
        max_msg_size=-1,
        storage="file",
        no_ack=False,
        template_owner=None,
        duplicate_window=10 * 60,
    )


def _jobs_stream_config() -> js.api.StreamConfig:
    return js.api.StreamConfig(
        name=constants.STREAM_JOBS,
        description="Events related to Xarta job events.",
        subjects=["jobs.>"],
        retention="limits",
        max_consumers=-1,
        max_msgs=1_000_000,
        max_bytes=5 * 1024 * 1024 * 1024,
        discard="old",
        max_age=7 * 24 * 60 * 60,
        max_msgs_per_subject=-1,
        max_msg_size=-1,
        storage="file",
        no_ack=False,
        template_owner=None,
        duplicate_window=10 * 60,
    )


def _documents_stream_config() -> js.api.StreamConfig:
    return js.api.StreamConfig(
        name=constants.STREAM_DOCUMENTS,
        description="Events related to Xarta document events.",
        subjects=["documents.>"],
        retention="limits",
        max_consumers=-1,
        max_msgs=10_000_000,
        max_bytes=10 * 1024 * 1024 * 1024,
        discard="old",
        max_age=30 * 24 * 60 * 60,
        max_msgs_per_subject=-1,
        max_msg_size=-1,
        storage="file",
        no_ack=False,
        template_owner=None,
        duplicate_window=10 * 60,
    )


def _archive_commands_stream_config() -> js.api.StreamConfig:
    return js.api.StreamConfig(
        name=constants.STREAM_ARCHIVE_COMMANDS,
        description="Durable archive storage deletion commands.",
        subjects=["archive.commands.delete"],
        retention="workqueue",
        max_consumers=-1,
        max_msgs=-1,
        max_bytes=10 * 1024 * 1024 * 1024,
        discard="new",
        max_age=0,
        max_msgs_per_subject=-1,
        max_msg_size=-1,
        storage="file",
        no_ack=False,
        template_owner=None,
        duplicate_window=10 * 60,
    )
