r"""
Database utilities and models related to the document type service
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import orjson

from xarta.db.postgresql import SanicPostgresModel
from xarta.protocol.document.type import DocumentType
from xarta.protocol.document.type import DocumentTypeIdentifier
from xarta.protocol.template.engine import TemplateEngineIdentifier

if TYPE_CHECKING:
    from typing import Final


class DocumentTypePostgresModel(SanicPostgresModel):

    BJSON_FIELDS: Final[set] = {
        "default_business_data",
        "metadata",
        "default_template_engine_options",
        "default_metadata",
    }

    QUERY_FIND_ONE: Final[str] = (
        "SELECT "
        "_id, identifier, metadata, default_business_data, default_template_engine, default_template_engine_options, default_retention, default_content_type, default_metadata "
        "FROM document_types WHERE identifier = $1"
    )

    QUERY_FIND_MANY: Final[str] = (
        "SELECT "
        "_id, identifier, metadata, default_business_data, default_template_engine, default_template_engine_options, default_retention, default_content_type, default_metadata "
        "FROM document_types WHERE ($1::text IS NULL OR identifier > $1) "
        "ORDER BY identifier LIMIT $2"
    )

    @classmethod
    def _document(cls, source) -> DocumentType:
        record = dict(source)
        for field in cls.BJSON_FIELDS:
            record[field] = orjson.loads(record[field])

        identifier = DocumentTypeIdentifier(record.pop("identifier"))
        template_engine = TemplateEngineIdentifier(
            record.pop("default_template_engine")
        )
        return DocumentType(
            identifier=identifier, default_template_engine=template_engine, **record
        )

    @classmethod
    async def find_one(cls, identifier: DocumentTypeIdentifier) -> DocumentType | None:
        async with cls.pool().acquire() as connection:
            record = await connection.fetchrow(cls.QUERY_FIND_ONE, identifier.value)

        if not record:
            return None

        return cls._document(record)

    @classmethod
    async def find_many(
        cls, *, limit: int, after: str | None = None
    ) -> tuple[DocumentType, ...]:
        async with cls.pool().acquire() as connection:
            records = await connection.fetch(cls.QUERY_FIND_MANY, after, limit)
        return tuple(cls._document(record) for record in records)
