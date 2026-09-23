from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.tracking import Reduction

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class SearchUpdate:
    outcome: str
    source_id: str
    details: Mapping[str, Any] | None = None


def reduce_search(current_state: Mapping[str, Any], update: SearchUpdate) -> Reduction:
    if current_state.get("status") in {
        "indexed",
        "unchanged",
        "unsupported_content",
        "index_rejected",
    }:
        return Reduction(dict(current_state), (), True)
    return Reduction(
        state={**current_state, "status": update.outcome},
        outcomes=(
            OutcomeEmission(
                outcome=update.outcome,
                subject=OutcomeSubject(kind="document", id=update.source_id),
                details=update.details,
            ),
        ),
        operation_resolved=True,
    )
