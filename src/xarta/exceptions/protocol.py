r"""
Specific document protocol related exceptions.
"""

from __future__ import annotations

import datetime

from typing import TYPE_CHECKING

from sanic.exceptions import SanicException

if TYPE_CHECKING:

    from xarta.protocol.dag.outcome import OutcomeEmission


class HTTPClientError(Exception):
    def __init__(self, endpoint: str, status: int):
        super().__init__(f"Endpoint {endpoint} returned {status}")
        self.endpoint = endpoint
        self.status = status


class TemporaryError(Exception):
    def __init__(
        self,
        message: str | bytes | None = None,
        delay: float | None = None,
        outcome: OutcomeEmission | None = None,
        max_deliver: int | None = None,
        classification: str = "retry_exhausted",
        error_code: str = "temporary_error",
        auto_replay: bool = False,
        backoff_multiplier: float = 2,
        max_delay: float = 300,
    ):
        super().__init__(message)
        self.message = message
        self.delay = delay  # In seconds
        self.outcome = outcome
        self.max_deliver = max_deliver
        self.classification = classification
        self.error_code = error_code
        self.auto_replay = auto_replay
        self.backoff_multiplier = backoff_multiplier
        self.max_delay = max_delay

    def retry_delay(self, delivery: int) -> float | None:
        if self.delay is None:
            return None
        try:
            delay = self.delay * self.backoff_multiplier ** max(delivery - 1, 0)
        except OverflowError:
            return self.max_delay
        return min(delay, self.max_delay)


class PermanentError(Exception):
    def __init__(
        self,
        message: str | bytes | None = None,
        *,
        classification: str = "permanent",
        error_code: str = "permanent_error",
        outcome: OutcomeEmission | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.classification = classification
        self.error_code = error_code
        self.outcome = outcome


class UnknownDAGNode(Exception):
    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind


class InvalidEmailProfile(Exception):
    def __init__(self, profile: str):
        super().__init__()
        self.profile = profile


class InvalidTemplateEngine(SanicException):
    def __init__(
        self,
        message: str | bytes | None = None,
    ):
        super().__init__(
            message=message,
            status_code=400,  # Bad Request
        )


class InvalidDocumentType(SanicException):
    def __init__(
        self,
        message: str | bytes | None = None,
    ):
        super().__init__(
            message=message,
            status_code=404,  # Document type could not be found
        )


class ArchiveDocumentNotFound(SanicException):
    def __init__(
        self,
        message: str | bytes | None = None,
    ):
        super().__init__(
            message=message,
            status_code=404,
        )


class UnsupportedPayloadContentType(SanicException):
    def __init__(
        self,
        message: str | bytes | None = None,
    ):
        super().__init__(
            message=message,
            status_code=400,  # Bad Request
        )


class BundleDocumentFilenameNotUniqueError(SanicException):
    def __init__(self, filename: str):
        super().__init__(
            message="Provided filename in the bundle request is not unique.",
            extra={"filename": filename},
            status_code=400,
        )


class UnsafeArchiveDeleteError(SanicException):
    def __init__(self, expires: datetime.datetime):
        super().__init__(
            message="Cannot safely delete documents that are not expired.",
            extra={"expires": expires.isoformat()},
            status_code=403,
        )
