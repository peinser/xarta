r"""
Database utilities and models related to the archive documents exporation jobs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta.db.postgresql import SanicPostgresModel

if TYPE_CHECKING:
    from typing import Final


class ArchivePostgresModel(SanicPostgresModel):
    ENV_PREFIX = "ARCHIVE"
    BJSON_FIELDS: Final[set] = {"metadata"}

    QUERY_FIND_EXPIRED: Final[str] = (
        "SELECT aggregate.archive, aggregate.document_id "
        "FROM archive_documents aggregate "
        "JOIN archive_document_versions version "
        "ON version._id = aggregate.head_version_id "
        "WHERE version.expires <= NOW() AND aggregate.lifecycle = 'active' "
        "ORDER BY version.expires, aggregate.archive, aggregate.document_id LIMIT $1"
    )

    @classmethod
    async def expired(cls, limit: int = 10000) -> list[tuple[str, str]]:
        r"""Fetches the set of expired documents from the archive."""
        async with cls.pool().acquire() as connection:
            return [
                (record["archive"], str(record["document_id"]))
                for record in await connection.fetch(cls.QUERY_FIND_EXPIRED, limit)
            ]
