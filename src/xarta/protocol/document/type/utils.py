r"""
Convenient utilities surrounding document types.
"""

from __future__ import annotations

from xarta import cache
from xarta.http.sessions import HTTPRequestManager

from .base import DocumentType
from .base import DocumentTypeIdentifier
from .constants import DOCUMENT_TYPE_CACHE_NAMESPACE_READ
from .constants import DOCUMENT_TYPE_CACHE_NAMESPACE_READ_TTL
from .constants import DOCUMENT_TYPE_SERVICE_ENDPOINT
from .constants import DOCUMENT_TYPE_SERVICE_TIMEOUT


async def fetch(identifier: DocumentTypeIdentifier | str) -> DocumentType | None:
    # Convert to a string representation for convenient retrieval.
    if isinstance(identifier, DocumentTypeIdentifier):
        identifier = identifier.value

    # Check if the document type has been requested recently.
    cached = await cache.manager.in_memory.get(
        identifier, namespace=DOCUMENT_TYPE_CACHE_NAMESPACE_READ
    )
    if cached:
        return DocumentType.fromdict(cached)

    async with HTTPRequestManager.__session__.get(
        f"{DOCUMENT_TYPE_SERVICE_ENDPOINT}/{identifier}",
        timeout=DOCUMENT_TYPE_SERVICE_TIMEOUT,
    ) as response:
        if response.status != 200:
            return None

        raw = await response.json()

        await cache.manager.in_memory.set(
            key=identifier,
            value=raw,
            namespace=DOCUMENT_TYPE_CACHE_NAMESPACE_READ,
            ttl=DOCUMENT_TYPE_CACHE_NAMESPACE_READ_TTL,
        )

        return DocumentType.fromdict(raw)
