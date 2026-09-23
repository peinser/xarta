from __future__ import annotations

import datetime

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import aiohttp
import orjson

from xarta.concurrency import bounded_map
from xarta.exceptions.protocol import HTTPClientError
from xarta.http.sessions import HTTPRequestManager

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class ArchiveSubmission:
    provider_reference: str | None
    state: Mapping[str, Any]


@dataclass(frozen=True)
class ArchiveRepresentationSubmission:
    representation_id: UUID
    content_type: str
    data: bytes
    name: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ArchiveVersionSubmission:
    archive: str
    document_id: UUID
    version_id: UUID
    parent_version_id: UUID | None
    created: datetime.datetime
    expires: datetime.datetime | None
    document_type: str | None
    metadata: dict[str, Any]
    default_representation_id: UUID
    representations: tuple[ArchiveRepresentationSubmission, ...]


class ArchiveAdapter(Protocol):
    async def submit(
        self,
        *,
        idempotency_key: str,
        versions: list[ArchiveVersionSubmission],
    ) -> ArchiveSubmission: ...


class ArchiveTransportError(Exception):
    """A failure which is safe to retry with the same idempotency key."""


def _manifest(version: ArchiveVersionSubmission) -> tuple[dict[str, Any], list[str]]:
    fields = [
        f"representation-{index}" for index in range(len(version.representations))
    ]
    return (
        {
            "document_id": str(version.document_id),
            "version_id": str(version.version_id),
            "parent_version_id": (
                str(version.parent_version_id) if version.parent_version_id else None
            ),
            "created": version.created.isoformat(),
            "expires": version.expires.isoformat() if version.expires else None,
            "document_type": version.document_type,
            "metadata": version.metadata,
            "default_representation_id": str(version.default_representation_id),
            "representations": [
                {
                    "representation_id": str(representation.representation_id),
                    "name": representation.name,
                    "content_type": representation.content_type,
                    "metadata": representation.metadata,
                    "field": field,
                }
                for representation, field in zip(
                    version.representations, fields, strict=True
                )
            ],
        },
        fields,
    )


def _multipart(version: ArchiveVersionSubmission) -> aiohttp.FormData:
    manifest, fields = _manifest(version)
    form = aiohttp.FormData()
    form.add_field(
        "manifest",
        orjson.dumps(manifest).decode(),
        content_type="application/json",
    )
    for representation, field in zip(version.representations, fields, strict=True):
        form.add_field(
            field,
            representation.data,
            filename=representation.name or str(representation.representation_id),
            content_type=representation.content_type,
        )
    return form


def _success_result(payload: Any, version: ArchiveVersionSubmission) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("document_id") != str(
        version.document_id
    ):
        raise ValueError("Archive response contains an invalid identifier")
    outcome = payload.get("outcome")
    if outcome not in {"created", "version_created", "unchanged"}:
        raise ValueError("Archive response contains an invalid outcome")
    try:
        resolved_version_id = str(UUID(payload["version_id"]))
    except (KeyError, TypeError, ValueError) as ex:
        raise ValueError("Archive response contains an invalid identifier") from ex
    if outcome in {"created", "version_created"} and resolved_version_id != str(
        version.version_id
    ):
        raise ValueError("Archive response contains an invalid identifier")
    return {
        "document_id": str(version.document_id),
        "version_id": resolved_version_id,
        "outcome": outcome,
    }


class XartaHTTPArchiveAdapter:
    def __init__(
        self,
        configuration: Mapping[str, Any],
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        XartaHTTPArchiveAdapterFactory.validate(configuration)
        self.endpoint = str(configuration["endpoint"]).rstrip("/")
        self.timeout = float(configuration["timeout"])
        self.concurrency = configuration.get("concurrency", 10)
        self._http_session = http_session

    async def _submit_version(
        self,
        session: aiohttp.ClientSession,
        idempotency_key: str,
        version: ArchiveVersionSubmission,
    ) -> tuple[dict[str, str], dict[str, str] | None]:
        url = f"{self.endpoint}/archives/{quote(version.archive, safe='')}/documents"
        request_id = f"{idempotency_key}:{version.document_id}:{version.version_id}"
        try:
            async with session.put(
                url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                data=_multipart(version),
                headers={"Idempotency-Key": request_id},
            ) as response:
                body = await response.read()
        except (
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ) as ex:
            raise ArchiveTransportError("Archive endpoint is unavailable") from ex

        if response.status in {408, 425, 429} or response.status >= 500:
            raise ArchiveTransportError(
                f"Archive endpoint returned retryable status {response.status}"
            )
        if response.status == 409:
            try:
                conflict = orjson.loads(body)
            except orjson.JSONDecodeError:
                conflict = {}
            outcome = conflict.get("outcome", "version_conflict")
            if outcome not in {"overwrite_rejected", "version_conflict"}:
                outcome = "version_conflict"
            result = {
                "document_id": str(version.document_id),
                "version_id": str(version.version_id),
                "outcome": outcome,
            }
            return result, {
                **result,
                "error": str(conflict.get("error", "Archive rejected the version")),
            }
        if response.status not in {200, 201}:
            raise HTTPClientError(endpoint=self.endpoint, status=response.status)
        try:
            return _success_result(orjson.loads(body), version), None
        except orjson.JSONDecodeError as ex:
            raise ValueError("Archive response must contain valid JSON") from ex

    async def submit(
        self,
        *,
        idempotency_key: str,
        versions: list[ArchiveVersionSubmission],
    ) -> ArchiveSubmission:
        session = self._http_session or HTTPRequestManager.__session__
        if session is None:
            raise RuntimeError("HTTP request manager has not been initialized")
        groups: dict[tuple[str, UUID], list[tuple[int, ArchiveVersionSubmission]]] = {}
        for index, version in enumerate(versions):
            groups.setdefault((version.archive, version.document_id), []).append(
                (index, version)
            )

        async def submit_group(
            group: list[tuple[int, ArchiveVersionSubmission]],
        ) -> list[tuple[int, dict[str, str], dict[str, str] | None]]:
            completed = []
            for index, version in group:
                result, error = await self._submit_version(
                    session, idempotency_key, version
                )
                completed.append((index, result, error))
            return completed

        completed_groups = await bounded_map(
            groups.values(), self.concurrency, submit_group
        )
        completed = {
            index: (result, error)
            for group in completed_groups
            for index, result, error in group
        }
        ordered = [completed[index] for index in range(len(versions))]
        results = [result for result, _ in ordered]
        outcomes = [result["outcome"] for result in results]
        errors = [error for _, error in ordered if error is not None]

        return ArchiveSubmission(
            provider_reference=idempotency_key,
            state={
                "documents": "rejected" if errors else "stored",
                "outcomes": outcomes,
                "results": results,
                "errors": errors,
            },
        )


class XartaHTTPArchiveAdapterFactory:
    def __init__(self, http_session: aiohttp.ClientSession | None = None) -> None:
        self._http_session = http_session

    @staticmethod
    def validate(configuration: Mapping[str, Any]) -> None:
        endpoint = configuration.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("xarta-http-archive requires a non-empty endpoint")
        timeout = configuration.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise ValueError("xarta-http-archive timeout must be a positive number")
        if timeout <= 0:
            raise ValueError("xarta-http-archive timeout must be a positive number")
        if configuration.get("concurrency", 10) < 1:
            raise ValueError("xarta-http-archive concurrency must be positive")

    def create(self, configuration: Mapping[str, Any]) -> ArchiveAdapter:
        return XartaHTTPArchiveAdapter(configuration, self._http_session)
