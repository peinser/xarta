from __future__ import annotations

import datetime

from uuid import uuid4

import xarta.protocol.dag

from xarta.protocol.dag.node import Node
from xarta.protocol.document import Document
from xarta.protocol.document.archive import ArchiveDocumentVersion
from xarta.protocol.document.archive import ArchiveRepresentation
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


def test_archive_version_round_trip_keeps_representation_metadata_separate() -> None:
    representation_id = uuid4()
    document = ArchiveDocumentVersion(
        identifier=uuid4(),
        version_id=uuid4(),
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

    restored = ArchiveDocumentVersion.fromdict(document.dict())

    assert restored.document_type is None
    assert restored.identifier == document.identifier
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
