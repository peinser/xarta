from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CapacityDecision(StrEnum):
    ADMIT = "admit"
    QUEUE = "queue"
    REJECT = "reject"


class CapacityOverflow(StrEnum):
    QUEUE = "queue"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CapacityPool:
    id: str
    timezone: str
    daily_admission_limit: int
    overflow: CapacityOverflow

    def __post_init__(self) -> None:
        if self.daily_admission_limit < -1:
            raise ValueError("Daily capacity must be -1, zero, or a positive integer")
        if not self.id or not self.timezone:
            raise ValueError("Capacity pool ID and timezone must be non-empty")


def capacity_decision(
    pool: CapacityPool, *, admitted: int, requested: int = 1
) -> CapacityDecision:
    if admitted < 0 or requested < 1:
        raise ValueError("Admitted must not be negative and requested must be positive")
    limit = pool.daily_admission_limit
    if limit == -1 or admitted + requested <= limit:
        return CapacityDecision.ADMIT
    if pool.overflow is CapacityOverflow.QUEUE:
        return CapacityDecision.QUEUE
    return CapacityDecision.REJECT
