r"""
Generic NATS utilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from nats.aio.msg import Msg


def extract_flow(msg: Msg) -> tuple[UUID | None, dict[str, str]]:
    headers = dict(msg.headers or {})
    value = headers.get("flow-id")
    return (UUID(value) if value else None), headers
