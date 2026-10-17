from __future__ import annotations

import datetime

from uuid import uuid4

import pytest

import xarta.protocol.dag

from xarta.protocol.dag.node import Node
from xarta.protocol.document import Document
from xarta.protocol.document.archive import ArchiveDocumentVersion
from xarta.protocol.document.archive import ArchiveRepresentation
from xarta.protocol.document.archive import validate_archive_name
from xarta.protocol.document.request.flow import DocumentFlowRequest
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEngineKind
from xarta.protocol.template.engine import TemplateEnginesOptions


def test_document_created_default_is_current_and_timezone_aware() -> None:
    before = datetime.datetime.now(tz=datetime.UTC)
    document = Document(identifier=uuid4(), content_type="text/plain")
    after = datetime.datetime.now(tz=datetime.UTC)

    assert before <= document.created <= after
    assert document.created.tzinfo is datetime.UTC


@pytest.mark.parametrize("value", ["default", "Payroll.EU", "archive-2026_10", "7"])
def test_archive_name_validation_preserves_identity(value):
    assert validate_archive_name(value) == value


@pytest.mark.parametrize(
    "value",
    [None, 7, True, "", " ", ".hidden", "../archive", "a/b", "é", "archive\n", "a\0b"],
)
def test_archive_name_validation_rejects_invalid_names(value):
    with pytest.raises(ValueError, match="archive"):
        validate_archive_name(value)


def test_archive_version_round_trip_keeps_representation_metadata_separate() -> None:
    representation_id = uuid4()
    document = ArchiveDocumentVersion(
        identifier=uuid4(),
        version_id=uuid4(),
        parent_version_id=uuid4(),
        default_representation_id=representation_id,
        metadata={"invoice": "2026-0042"},
        representations=(
            ArchiveRepresentation(
                representation_id=representation_id,
                content_type="application/pdf",
                metadata={"generator": "renderer"},
                checksum="ab" * 64,
                size=10,
            ),
        ),
    )

    serialized = document.dict()
    assert serialized["version"] == {
        "id": str(document.version_id),
        "parent": str(document.parent_version_id),
    }
    assert "version_id" not in serialized
    assert "parent_version_id" not in serialized
    assert serialized["representations"]["default"] == str(representation_id)
    item = serialized["representations"]["items"][0]
    assert item["id"] == str(representation_id)
    assert item["content"] == {
        "type": "application/pdf",
        "checksum": {"sha512": "ab" * 64},
        "size_bytes": 10,
        "availability": "available",
    }
    assert serialized["content_availability"] == "available"
    assert serialized["created_at"] == document.created.isoformat()
    assert serialized["expires_at"] is None
    restored = ArchiveDocumentVersion.fromdict(serialized)

    assert restored.document_type is None
    assert restored.identifier == document.identifier
    assert restored.version_id == document.version_id
    assert restored.parent_version_id == document.parent_version_id
    assert restored.default_representation.metadata == {"generator": "renderer"}
    assert restored.metadata == {"invoice": "2026-0042"}


def test_template_engine_options_expose_identifiers_and_kinds() -> None:
    options = TemplateEnginesOptions(
        {"primary": {"kind": "jinja", "template": {"path": "invoice.html"}}}
    )

    assert options.engines == [TemplateEngineIdentifier("primary")]
    assert options.engine_options[0].template_engine_kind is TemplateEngineKind.JINJA
    assert options.engine_options[0].template_path == "invoice.html"


def test_dag_parser_assigns_parent_links_and_terminal_state() -> None:
    root = xarta.protocol.dag.parse(
        {"kind": "debug", "on": {"success": [{"kind": "debug"}]}}
    )

    child = root.on["success"][0]
    assert child.parent is root
    assert root.terminal() is False
    assert child.terminal() is True


def test_flow_request_preserves_identifiers_during_round_trip() -> None:
    request = DocumentFlowRequest(dag=Node(kind="debug"))

    restored = DocumentFlowRequest.fromdict(request.dict())

    assert restored.id == request.id
    assert restored.correlation_id == request.correlation_id
    assert restored.dag.kind == request.dag.kind
