r"""
Constants for the Archive DAG protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta import env

if TYPE_CHECKING:
    from typing import Final


ARCHIVE_SERVICE_ENDPOINT: Final[str] = env.extract(
    key="ARCHIVE_SERVICE_ENDPOINT",
    dtype=str,
)


ARCHIVE_SERVICE_TIMEOUT: Final[float] = env.extract(
    key="ARCHIVE_SERVICE_TIMEOUT",
    default="30.0",
    dtype=float,
)
