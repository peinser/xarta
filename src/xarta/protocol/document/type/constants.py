r"""
Constants tied to document types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta import env

if TYPE_CHECKING:
    from typing import Final


DOCUMENT_TYPE_SERVICE_ENDPOINT: Final[str] = env.extract(
    key="DOCUMENT_TYPE_SERVICE_ENDPOINT",
    dtype=str,
)


DOCUMENT_TYPE_SERVICE_TIMEOUT: Final[float] = env.extract(
    key="DOCUMENT_TYPE_SERVICE_TIMEOUT",
    default="5.0",
    dtype=float,
)


DOCUMENT_TYPE_CACHE_NAMESPACE: Final[str] = "document-type"
DOCUMENT_TYPE_CACHE_NAMESPACE_READ: Final[str] = f"{DOCUMENT_TYPE_CACHE_NAMESPACE}:read"
DOCUMENT_TYPE_CACHE_NAMESPACE_READ_TTL: Final[float] = 300.0
