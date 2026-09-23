from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.tracking import Reduction

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class SFTPUpdate:
    outcome: str
    document_id: str
    details: Mapping[str, Any] | None = None


def reduce_sftp(current_state: Mapping[str, Any], update: SFTPUpdate) -> Reduction:
    if current_state.get("status") in {
        "uploaded",
        "path_conflict",
        "upload_rejected",
        CommonOutcome.OUTCOME_UNCERTAIN,
    }:
        return Reduction(
            state=dict(current_state), outcomes=(), operation_resolved=True
        )
    return Reduction(
        state={**current_state, "status": update.outcome},
        outcomes=(
            OutcomeEmission(
                outcome=update.outcome,
                subject=OutcomeSubject(kind="document", id=update.document_id),
                details=update.details,
            ),
        ),
        operation_resolved=True,
    )
