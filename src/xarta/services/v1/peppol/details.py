from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

import orjson

from xarta.protocol.dag import sanitize_outcome_message

if TYPE_CHECKING:
    from collections.abc import Iterable

    from xarta.services.v1.peppol.models import PeppolFailure
    from xarta.services.v1.peppol.models import PeppolValidationIssue


MAX_VALIDATION_ISSUES = 10
MAX_OUTCOME_DETAILS_BYTES = 16 * 1024


def sanitize_provider_message(message: str | None) -> str | None:
    return sanitize_outcome_message(message)


def validation_details(issues: Iterable[PeppolValidationIssue]) -> dict:
    all_issues = tuple(issues)
    recorded = [
        {
            "rule": issue.rule,
            "severity": issue.severity,
            "message": sanitize_provider_message(issue.message),
        }
        for issue in all_issues[:MAX_VALIDATION_ISSUES]
    ]
    details = {
        "issues": recorded,
        "total_issue_count": len(all_issues),
        "recorded_issue_count": len(recorded),
        "truncated": len(recorded) != len(all_issues),
    }
    while len(orjson.dumps(details)) > MAX_OUTCOME_DETAILS_BYTES and recorded:
        recorded.pop()
        details["recorded_issue_count"] = len(recorded)
        details["truncated"] = True
    return details


def failure_details(
    failure: PeppolFailure,
    *,
    provider_document_id: str | None = None,
    provider_state: str | None = None,
) -> dict:
    details: dict[str, Any] = {
        "provider": failure.provider,
        "stage": failure.stage.value,
        "category": failure.category.value,
    }
    optional = {
        "provider_document_id": provider_document_id,
        "provider_state": provider_state,
        "provider_code": failure.provider_code,
        "message": sanitize_provider_message(failure.provider_message),
        "http_status": failure.http_status,
    }
    details.update({key: value for key, value in optional.items() if value is not None})
    return details
