r"""Base NATS integration and utilities."""

from __future__ import annotations

import nats

import xarta


class BaseNATSModel:
    r"""
    Base representation of a NATS model that
    provides all utility methods. This class
    serves as an entry to setup the necessary
    NATS and Jetstream connection details.
    """

    @staticmethod
    async def open(**kwargs) -> nats.aio.client.Client:
        configured_servers = xarta.env.extract("NATS_SERVERS", optional=False)
        servers = [
            server.strip() for server in configured_servers.split(",") if server.strip()
        ]
        if not servers:
            raise ValueError("NATS_SERVERS must contain at least one server URL")

        return await nats.connect(servers=servers, **kwargs)
