from __future__ import annotations

from uuid import uuid4

import jsonschema
import pytest

from xarta.protocol.dag import parse as parse_node
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.document.source import ArchiveDocumentSource
from xarta.protocol.document.source import TemporaryDocumentSource
from xarta.protocol.document.source import parse
from xarta.services.v1.intake.capabilities import CapabilityManifest


@pytest.mark.parametrize("source", [{}, {"source": "temporary"}])
def test_default_and_explicit_temporary_reference(source):
    identifier = uuid4()
    reference = parse(id=str(identifier), **source)
    assert isinstance(reference, TemporaryDocumentSource)
    assert reference.id == identifier
    assert reference.source == "temporary"


@pytest.mark.parametrize("source", ["generate", "scratch", "unknown", None, ""])
def test_obsolete_or_unknown_sources_are_not_aliases(source):
    with pytest.raises(ValueError, match="Unknown document source"):
        parse(source=source, id=str(uuid4()))


@pytest.mark.parametrize("field", ["version", "representation", "archive"])
def test_temporary_reference_rejects_archive_fields(field):
    with pytest.raises(ValueError, match="only accept source and id"):
        parse(id=str(uuid4()), **{field: str(uuid4())})


@pytest.mark.parametrize("kind", ["peppol", "search-index", "signature"])
def test_consumers_accept_omitted_source_in_parser_and_schema(kind):
    reference = {"id": str(uuid4())}
    value = {"kind": kind, "document": reference}
    if kind == "search-index":
        value["destination"] = "documents"
    if kind == "signature":
        value = {
            "kind": kind,
            "documents": [{"document": reference, "out": str(uuid4())}],
        }
    node = parse_node(value)
    interpreted = node.interpret()
    if kind == "signature":
        interpreted = interpreted[0].source
    assert isinstance(interpreted, TemporaryDocumentSource)
    jsonschema.validate({"dag": value}, CapabilityManifest(frozenset({kind})).schema())


def test_signing_accepts_archive_and_rejects_legacy_or_colliding_outputs():
    identifier = str(uuid4())
    reference = {"source": "archive", "id": identifier, "version": str(uuid4())}
    node = SignatureNode(documents=[{"document": reference, "out": str(uuid4())}])
    assert isinstance(node.interpret()[0].source, ArchiveDocumentSource)
    assert parse_node(node.dict()).dict() == node.dict()
    with pytest.raises(ValueError, match="document and out"):
        SignatureNode(documents=[{"in": identifier, "out": str(uuid4())}])
    with pytest.raises(ValueError, match="overwrite"):
        SignatureNode(documents=[{"document": {"id": identifier}, "out": identifier}])
    with pytest.raises(ValueError, match="unique"):
        SignatureNode(
            documents=[{"document": {"id": str(uuid4())}, "out": identifier}] * 2
        )
