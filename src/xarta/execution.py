from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    """Durability required by a selected capability implementation."""

    SYNCHRONOUS = "synchronous"
    TRACKED = "tracked"
