r"""
Constants used throughout NATS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Final


STREAM_REQUESTS: Final[str] = "REQUESTS"
STREAM_DOCUMENTS: Final[str] = "DOCUMENTS"
STREAM_JOBS: Final[str] = "JOBS"
STREAM_ARCHIVE_COMMANDS: Final[str] = "ARCHIVE_COMMANDS"
