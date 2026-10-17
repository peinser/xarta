from __future__ import annotations

import datetime
import hashlib

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import UUID
from uuid import uuid4

import orjson
import pytest

from sanic.headers import parse_accept

from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import InternalServerError
from xarta.protocol.document.archive import ArchiveDocumentVersion
from xarta.protocol.document.archive import ArchiveRepresentation
from xarta.services.v1.archive.base import archive_exception
from xarta.services.v1.archive.base import bp
from xarta.services.v1.archive.db import ArchivePostgresModel
from xarta.services.v1.archive.db import StorageReference
from xarta.services.v1.archive.db import VersionPage
from xarta.services.v1.archive.delete import _safe
from xarta.services.v1.archive.details import _decode_cursor
from xarta.services.v1.archive.details import retrieve_versions
from xarta.services.v1.archive.download import _content_disposition
from xarta.services.v1.archive.download import _retrieve
from xarta.services.v1.archive.download import _retrieve_exact
from xarta.services.v1.archive.download import _select_representation
from xarta.services.v1.archive.storage import FilesystemStorageBackend
from xarta.services.v1.archive.store import _parse_snapshot


class Request(SimpleNamespace):
    @property
    def accept(self):
        return parse_accept(self.headers.get("accept"))


def reference(
    content_type: str,
    *,
    default: bool = False,
    name: str | None = None,
    content: bytes = b"content",
) -> StorageReference:
    return StorageReference(
        version_id=uuid4(),
        representation_id=uuid4(),
        name=name,
        backend="filesystem",
        backend_revision="v1",
        storage_key=str(uuid4()),
        content_type=content_type,
        checksum=hashlib.sha512(content).digest(),
        size=len(content),
        default=default,
    )


def version(document_id: UUID, version_id: UUID, created: datetime.datetime):
    representation_id = uuid4()
    return ArchiveDocumentVersion(
        identifier=document_id,
        archive="payroll",
        version_id=version_id,
        parent_version_id=None,
        document_type=None,
        metadata={},
        created=created,
        expires=None,
        default_representation_id=representation_id,
        representations=(
            ArchiveRepresentation(
                representation_id=representation_id,
                content_type="text/plain",
                checksum="ab" * 64,
                size=4,
            ),
        ),
    )


def snapshot_request(manifest: dict, files: dict[str, bytes]):
    target = SimpleNamespace(
        backend_name="primary",
        backend_revision="v1",
        backend=FilesystemStorageBackend("archive"),
    )
    return Request(
        files={name: SimpleNamespace(body=data) for name, data in files.items()},
        form={"manifest": orjson.dumps(manifest)},
        headers={},
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                archive_storage=SimpleNamespace(resolve=lambda _archive: target)
            )
        ),
    )


def test_archive_routes_expose_head_versions_and_representations() -> None:
    routes = {route.uri for route in bp._future_routes}
    assert {
        "/archives/<archive:str>/documents/search",
        "/documents/search",
        "/archives/<archive:str>/documents/<document_id:uuid>/representations",
        "/archives/<archive:str>/documents/<document_id:uuid>/versions",
        "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>",
        "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>/details",
        "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>/representations",
        "/archives/<archive:str>/documents/<document_id:uuid>/versions/<version_id:uuid>/representations/<representation_id:uuid>",
    } <= routes
    assert not any(route.endswith("/metadata") for route in routes)


@pytest.mark.asyncio
async def test_manifest_parses_separate_version_and_representation_metadata() -> None:
    document_id = uuid4()
    version_id = uuid4()
    pdf_id = uuid4()
    xml_id = uuid4()
    request = snapshot_request(
        {
            "id": str(document_id),
            "version": {"id": str(version_id)},
            "created_at": "2030-01-01T00:00:00+00:00",
            "metadata": {"invoice_number": "2026-0042"},
            "representations": {
                "default": str(pdf_id),
                "items": [
                    {
                        "id": str(xml_id),
                        "field": "xml",
                        "content": {"type": "application/xml"},
                        "metadata": {"schema_version": "2.1"},
                    },
                    {
                        "id": str(pdf_id),
                        "field": "pdf",
                        "content": {"type": "application/pdf"},
                        "name": "invoice.pdf",
                        "metadata": {"generator": "renderer"},
                    },
                ],
            },
        },
        {"pdf": b"pdf", "xml": b"xml"},
    )
    request.headers["Idempotency-Key"] = "invoice-snapshot-0042"

    with patch.object(
        FilesystemStorageBackend,
        "representation_key_prefix",
        wraps=FilesystemStorageBackend.representation_key_prefix,
    ) as key_prefix:
        parsed = await _parse_snapshot(request, "payroll")
    key_prefix.assert_called_once_with("payroll", document_id, version_id)
    expected_prefix = FilesystemStorageBackend.representation_key_prefix(
        "payroll", document_id, version_id
    )
    allocation_ids = set()
    for item in parsed.representations:
        stem, allocation_id = item.write.storage_key.rsplit(".", 1)
        assert stem == f"{expected_prefix}{item.write.representation_id}"
        assert len(allocation_id) == 32
        assert UUID(allocation_id).version == 4
        allocation_ids.add(allocation_id)
    assert len(allocation_ids) == 1

    retry = await _parse_snapshot(request, "payroll")
    assert retry.write.document_id == parsed.write.document_id
    assert retry.write.version_id == parsed.write.version_id
    assert retry.write.idempotency_key == parsed.write.idempotency_key
    assert retry.write.fingerprint == parsed.write.fingerprint
    assert retry.write.snapshot_fingerprint == parsed.write.snapshot_fingerprint
    assert [item.representation_id for item in retry.write.representations] == [
        item.representation_id for item in parsed.write.representations
    ]
    assert {item.storage_key for item in retry.write.representations}.isdisjoint(
        item.storage_key for item in parsed.write.representations
    )

    assert parsed.write.metadata == {"invoice_number": "2026-0042"}
    assert parsed.write.default_representation_id == pdf_id
    assert [item.representation_id for item in parsed.write.representations] == sorted(
        [pdf_id, xml_id], key=str
    )
    metadata = {
        item.write.content_type: item.write.metadata for item in parsed.representations
    }
    assert metadata == {
        "application/pdf": {"generator": "renderer"},
        "application/xml": {"schema_version": "2.1"},
    }


@pytest.mark.asyncio
async def test_manifest_requires_explicit_default_for_multiple_representations() -> (
    None
):
    request = snapshot_request(
        {
            "representations": {
                "items": [
                    {"field": "one", "content": {"type": "application/pdf"}},
                    {"field": "two", "content": {"type": "application/xml"}},
                ]
            }
        },
        {"one": b"one", "two": b"two"},
    )
    with pytest.raises(BadRequestError, match="representations.default"):
        await _parse_snapshot(request, "default")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "   "}, "name"),
        ({"content": {"type": "pdf"}}, "content.type"),
        ({"content": {"type": "application/pdf", "size_bytes": 10}}, "Unsupported"),
        ({"representation_id": "ignored-legacy-id"}, "Unsupported"),
        ({"id": False}, "UUID string"),
        ({"metadata": []}, "JSON object"),
        ({"field": "missing"}, "missing multipart field"),
    ],
)
async def test_manifest_rejects_invalid_representation_fields(change, message) -> None:
    specification = {"field": "content", "content": {"type": "application/pdf"}}
    specification.update(change)
    request = snapshot_request(
        {"representations": {"items": [specification]}}, {"content": b"pdf"}
    )
    with pytest.raises(BadRequestError, match=message) as raised:
        await _parse_snapshot(request, "default")
    error = await archive_exception(request, raised.value)
    assert error.status == 400
    assert orjson.loads(error.body) == {
        "error": {"code": "bad_request", "message": str(raised.value)}
    }


@pytest.mark.asyncio
async def test_version_collection_exposes_head_and_cursor(monkeypatch) -> None:
    document_id = uuid4()
    head_id = uuid4()
    created = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)
    item = version(document_id, head_id, created)
    find_versions = AsyncMock(
        return_value=VersionPage(
            head_version_id=head_id,
            items=(item,),
            has_more=True,
        )
    )
    monkeypatch.setattr(ArchivePostgresModel, "find_versions", find_versions)

    result = await retrieve_versions(
        Request(args={"limit": "1"}), "payroll", document_id
    )
    payload = orjson.loads(result.body)

    assert payload["head_version_id"] == str(head_id)
    assert "current_version_id" not in payload
    assert payload["pagination"]["limit"] == 1
    assert "next_cursor" not in payload
    assert _decode_cursor(payload["pagination"]["next"], "payroll", document_id) == (
        created,
        head_id,
    )


@pytest.mark.parametrize("value", ["", "invalid", "2", "true-ish"])
def test_delete_rejects_ambiguous_safety_values(value: str) -> None:
    with pytest.raises(BadRequestError):
        _safe(value)


@pytest.mark.parametrize("value", [None, "true", "1", "yes"])
def test_delete_defaults_to_safe(value: str | None) -> None:
    assert _safe(value) is True


@pytest.mark.parametrize("value", ["false", "0", "no"])
def test_delete_requires_an_explicit_unsafe_value(value: str) -> None:
    assert _safe(value) is False


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (None, "application/pdf"),
        ("*/*", "application/pdf"),
        ("application/pdf", "application/pdf"),
        ("application/xml", "application/xml"),
        ("text/*", "text/html"),
        ("application/xml;q=0.9,text/html;q=0.5", "application/xml"),
    ],
)
def test_accept_selection_uses_default_then_unique_alternative(
    accept, expected
) -> None:
    references = (
        reference("application/pdf", default=True),
        reference("application/xml"),
        reference("text/html"),
    )
    request = Request(headers={} if accept is None else {"accept": accept})
    selected = _select_representation(request, references)
    assert isinstance(selected, StorageReference)
    assert selected.content_type == expected


def test_accept_selection_returns_none_when_unavailable() -> None:
    selected = _select_representation(
        Request(headers={"accept": "image/tiff"}),
        (reference("application/pdf", default=True),),
    )
    assert selected is None


def test_accept_selection_does_not_order_ambiguous_identical_types() -> None:
    selected = _select_representation(
        Request(headers={"accept": "application/pdf"}),
        (
            reference("application/xml", default=True),
            reference("application/pdf"),
            reference("application/pdf"),
        ),
    )
    assert isinstance(selected, tuple)
    assert len(selected) == 2


@pytest.mark.asyncio
async def test_exact_representation_verifies_bytes_and_uses_safe_filename(
    monkeypatch,
) -> None:
    document_id = uuid4()
    content = b"archived document"
    item = reference(
        "text/plain",
        default=False,
        name='invoice "quoted"; 2026.txt',
        content=content,
    )
    monkeypatch.setattr(
        ArchivePostgresModel, "find_source", AsyncMock(return_value=item)
    )
    backend = SimpleNamespace(get=AsyncMock(return_value=content))
    request = Request(
        headers={},
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                archive_storage=SimpleNamespace(backend=lambda *_: backend)
            )
        ),
    )

    result = await _retrieve_exact(
        request, "default", document_id, item.version_id, item.representation_id
    )

    assert result.body == content
    assert result.headers["etag"] == f'"sha512-{item.checksum.hex()}"'
    disposition = result.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''invoice%20%22quoted%22%3B%202026.txt" in disposition
    assert "vary" not in result.headers


def test_unnamed_representation_has_deterministic_filename() -> None:
    document_id = uuid4()
    item = reference("image/tiff")
    assert _content_disposition(item, document_id) == _content_disposition(
        item, document_id
    )


@pytest.mark.asyncio
async def test_negotiated_download_returns_406(monkeypatch) -> None:
    monkeypatch.setattr(
        ArchivePostgresModel,
        "find_sources",
        AsyncMock(return_value=(reference("application/pdf", default=True),)),
    )
    result = await _retrieve(
        Request(headers={"accept": "image/tiff"}), "default", uuid4(), None
    )
    assert result.status == 406
    assert result.headers["vary"] == "Accept"
    assert orjson.loads(result.body) == {
        "error": {
            "code": "not_acceptable",
            "message": "requested content type is not available",
        }
    }


@pytest.mark.asyncio
async def test_download_rejects_corrupt_content(monkeypatch) -> None:
    item = reference("text/plain", default=True)
    monkeypatch.setattr(
        ArchivePostgresModel, "find_sources", AsyncMock(return_value=(item,))
    )
    backend = SimpleNamespace(get=AsyncMock(return_value=b"corrupt"))
    request = Request(
        headers={},
        app=SimpleNamespace(
            ctx=SimpleNamespace(
                archive_storage=SimpleNamespace(backend=lambda *_: backend)
            )
        ),
    )
    with pytest.raises(InternalServerError, match="integrity verification"):
        await _retrieve(request, "default", uuid4(), None)
