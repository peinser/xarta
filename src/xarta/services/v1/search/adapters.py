from __future__ import annotations

import hashlib
import re
import uuid

from typing import TYPE_CHECKING
from typing import Protocol

import aiohttp
import asyncpg  # type: ignore[import-untyped]
import orjson

from xarta.services.v1.search.models import ArchiveSearchSource
from xarta.services.v1.search.models import DocumentSearchSource
from xarta.services.v1.search.models import IndexSubmission

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from xarta.services.v1.search.models import SearchableDocument


_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SearchAdapter(Protocol):
    async def index(
        self, *, idempotency_key: str, document: SearchableDocument
    ) -> IndexSubmission: ...

    async def remove_archive_version(self, *, source: ArchiveSearchSource) -> None: ...

    async def remove_archive_document(
        self, *, archive: str, document_id: uuid.UUID
    ) -> None: ...


class SearchTransportError(Exception):
    pass


def _validate_configuration(configuration: Mapping[str, Any]) -> None:
    namespace = configuration.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("PostgreSQL search destination requires a valid namespace")


class PostgreSQLSearchAdapter:
    def __init__(self, pool: asyncpg.Pool, configuration: Mapping[str, Any]) -> None:
        _validate_configuration(configuration)
        self.pool = pool
        self.namespace = configuration["namespace"]

    async def index(
        self, *, idempotency_key: str, document: SearchableDocument
    ) -> IndexSubmission:
        record_id = uuid.UUID(idempotency_key)
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                archive_source = (
                    document.source
                    if isinstance(document.source, ArchiveSearchSource)
                    else None
                )
                generic_source = document.source if archive_source is None else None
                assert generic_source is None or isinstance(
                    generic_source, DocumentSearchSource
                )
                source_kind = (
                    "archive" if generic_source is None else generic_source.kind
                )
                inserted = await connection.fetchval(
                    """
                    INSERT INTO public.search_documents (
                        id, namespace, projection_revision, source_kind, source_archive,
                        source_document_id, source_version_id, source_id, source_version, digest,
                        content_type, document_type, metadata,
                        default_representation_id, representations
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13::jsonb, $14, $15::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    record_id,
                    self.namespace,
                    document.projection_revision,
                    source_kind,
                    archive_source.archive if archive_source else None,
                    archive_source.document_id if archive_source else None,
                    archive_source.version_id if archive_source else None,
                    str(generic_source.document_id) if generic_source else None,
                    generic_source.version if generic_source else None,
                    document.digest,
                    document.content_type,
                    document.document_type,
                    orjson.dumps(dict(document.metadata)).decode(),
                    document.default_representation_id,
                    orjson.dumps(
                        [
                            {
                                "representation_id": str(item.representation_id),
                                "name": item.name,
                                "content_type": item.content_type,
                                "metadata": dict(item.metadata),
                            }
                            for item in document.representations
                        ]
                    ).decode(),
                )
                if inserted is not None:
                    return IndexSubmission("indexed", str(inserted))

                existing = await connection.fetchrow(
                    """
                    SELECT id, digest, metadata FROM public.search_documents
                    WHERE namespace = $1 AND projection_revision = $2
                      AND source_kind = $3
                      AND source_archive IS NOT DISTINCT FROM $4
                      AND source_document_id IS NOT DISTINCT FROM $5
                      AND source_version_id IS NOT DISTINCT FROM $6
                      AND source_id IS NOT DISTINCT FROM $7
                      AND source_version IS NOT DISTINCT FROM $8
                    """,
                    self.namespace,
                    document.projection_revision,
                    source_kind,
                    archive_source.archive if archive_source else None,
                    archive_source.document_id if archive_source else None,
                    archive_source.version_id if archive_source else None,
                    str(generic_source.document_id) if generic_source else None,
                    generic_source.version if generic_source else None,
                )
                if existing is None:
                    raise SearchTransportError(
                        "Conflicting search document disappeared"
                    )
                if archive_source is not None or existing["digest"] == document.digest:
                    return IndexSubmission("unchanged", str(existing["id"]))
                return IndexSubmission(
                    "index_rejected",
                    str(existing["id"]),
                    {"reason": "source_version_digest_conflict"},
                )
        except (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            TimeoutError,
        ) as ex:
            raise SearchTransportError("PostgreSQL search index is unavailable") from ex

    async def remove_archive_version(self, *, source: ArchiveSearchSource) -> None:
        await self._execute(
            "DELETE FROM public.search_documents WHERE namespace = $1 "
            "AND source_kind = 'archive' AND source_archive = $2 "
            "AND source_document_id = $3 AND source_version_id = $4",
            self.namespace,
            source.archive,
            source.document_id,
            source.version_id,
        )

    async def remove_archive_document(
        self, *, archive: str, document_id: uuid.UUID
    ) -> None:
        await self._execute(
            "DELETE FROM public.search_documents WHERE namespace = $1 "
            "AND source_kind = 'archive' AND source_archive = $2 "
            "AND source_document_id = $3",
            self.namespace,
            archive,
            document_id,
        )

    async def _execute(self, query: str, *arguments: Any) -> None:
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(query, *arguments)
        except (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            TimeoutError,
        ) as ex:
            raise SearchTransportError("PostgreSQL search index is unavailable") from ex


class PostgreSQLSearchAdapterFactory:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    def validate(self, configuration: Mapping[str, Any]) -> None:
        _validate_configuration(configuration)

    def create(self, configuration: Mapping[str, Any]) -> SearchAdapter:
        return PostgreSQLSearchAdapter(self.pool, configuration)


class ElasticsearchSearchAdapter:
    """Indexes immutable projections through the ElasticSearch-compatible API."""

    def __init__(
        self, session: aiohttp.ClientSession, configuration: Mapping[str, Any]
    ) -> None:
        ElasticsearchSearchAdapterFactory.validate_configuration(configuration)
        self.session = session
        self.endpoint = str(configuration["endpoint"]).rstrip("/")
        self.index_name = str(configuration["index"])
        self.timeout = float(configuration.get("timeout", 10))
        self.headers = {"content-type": "application/json"}
        if configuration.get("api_key"):
            self.headers["authorization"] = f"ApiKey {configuration['api_key']}"
        self.auth = (
            aiohttp.BasicAuth(configuration["username"], configuration["password"])
            if configuration.get("username")
            else None
        )

    async def index(
        self, *, idempotency_key: str, document: SearchableDocument
    ) -> IndexSubmission:
        source_payload = self._source_payload(document.source)
        identity = "\0".join(
            (
                document.projection_revision,
                *(str(value) for value in source_payload.values()),
            )
        )
        document_id = hashlib.sha256(identity.encode()).hexdigest()
        payload = {
            "projection_revision": document.projection_revision,
            **source_payload,
            "content_type": document.content_type,
            "document_type": document.document_type,
            "metadata": dict(document.metadata),
            "default_representation_id": (
                str(document.default_representation_id)
                if document.default_representation_id
                else None
            ),
            "representations": [
                {
                    "representation_id": str(item.representation_id),
                    "name": item.name,
                    "content_type": item.content_type,
                    "metadata": dict(item.metadata),
                }
                for item in document.representations
            ],
            "idempotency_key": idempotency_key,
        }
        if document.digest is not None:
            payload["digest"] = document.digest
        try:
            async with self.session.put(
                f"{self.endpoint}/{self.index_name}/_create/{document_id}",
                data=orjson.dumps(payload),
                headers=self.headers,
                auth=self.auth,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status in {200, 201}:
                    return IndexSubmission("indexed", document_id)
                if response.status != 409:
                    raise SearchTransportError(
                        f"ElasticSearch index request returned {response.status}"
                    )

            async with self.session.get(
                f"{self.endpoint}/{self.index_name}/_doc/{document_id}",
                headers=self.headers,
                auth=self.auth,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    raise SearchTransportError(
                        "Conflicting ElasticSearch projection could not be read"
                    )
                existing = orjson.loads(await response.read())
            if (
                isinstance(document.source, ArchiveSearchSource)
                or existing.get("_source", {}).get("digest") == document.digest
            ):
                return IndexSubmission("unchanged", document_id)
            return IndexSubmission(
                "index_rejected",
                document_id,
                {"reason": "source_version_digest_conflict"},
            )
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise SearchTransportError("ElasticSearch is unavailable") from ex

    @staticmethod
    def _source_payload(source) -> dict[str, str]:
        if isinstance(source, ArchiveSearchSource):
            return {
                "source_kind": "archive",
                "source_archive": source.archive,
                "source_document_id": str(source.document_id),
                "source_version_id": str(source.version_id),
            }
        return {
            "source_kind": source.kind,
            "source_id": str(source.document_id),
            "source_version": source.version,
        }

    async def _request(
        self, method: str, url: str, payload: Mapping[str, Any], *, expected: set[int]
    ) -> None:
        try:
            request = getattr(self.session, method)
            async with request(
                url,
                data=orjson.dumps(payload),
                headers=self.headers,
                auth=self.auth,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status not in expected:
                    raise SearchTransportError(
                        f"ElasticSearch lifecycle request returned {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError) as ex:
            raise SearchTransportError("ElasticSearch is unavailable") from ex

    async def remove_archive_version(self, *, source: ArchiveSearchSource) -> None:
        await self._delete_by_query(self._archive_filters(source))

    async def remove_archive_document(
        self, *, archive: str, document_id: uuid.UUID
    ) -> None:
        await self._delete_by_query(
            self._archive_filters(
                ArchiveSearchSource(archive, document_id, uuid.UUID(int=0)),
                include_version=False,
            )
        )

    @staticmethod
    def _archive_filters(
        source: ArchiveSearchSource, *, include_version: bool = True
    ) -> list[dict[str, dict[str, str]]]:
        filters = [
            {"term": {"source_kind.keyword": "archive"}},
            {"term": {"source_archive.keyword": source.archive}},
            {"term": {"source_document_id.keyword": str(source.document_id)}},
        ]
        if include_version:
            filters.append(
                {"term": {"source_version_id.keyword": str(source.version_id)}}
            )
        return filters

    async def _delete_by_query(self, filters) -> None:
        await self._request(
            "post",
            f"{self.endpoint}/{self.index_name}/_delete_by_query?refresh=true",
            {"query": {"bool": {"filter": filters}}},
            expected={200},
        )


class ElasticsearchSearchAdapterFactory:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    @staticmethod
    def validate_configuration(configuration: Mapping[str, Any]) -> None:
        for name in ("endpoint", "index"):
            value = configuration.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ElasticSearch destination requires {name}")
        if bool(configuration.get("username")) != bool(configuration.get("password")):
            raise ValueError(
                "ElasticSearch destination requires both username and password"
            )
        timeout = configuration.get("timeout", 10)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or timeout <= 0
        ):
            raise ValueError("ElasticSearch destination timeout must be positive")

    def validate(self, configuration: Mapping[str, Any]) -> None:
        self.validate_configuration(configuration)

    def create(self, configuration: Mapping[str, Any]) -> SearchAdapter:
        return ElasticsearchSearchAdapter(self.session, configuration)
