from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import orjson
import pytest

from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.document.request.flow import DocumentFlowRequest

EXAMPLES = Path(__file__).parents[2] / "examples" / "protocol"


def test_full_peppol_fixture_uses_production_parser_and_round_trips() -> None:
    payload = orjson.loads((EXAMPLES / "peppol-e-invoice-be.json").read_bytes())
    request = DocumentFlowRequest.fromdict(payload)

    assert isinstance(request.dag, PeppolNode)
    assert request.dag.document["version"] == "91000000-0000-0000-0000-000000000201"
    assert DocumentFlowRequest.fromdict(request.dict()).dag.dict() == request.dag.dict()


@pytest.mark.parametrize("field", ["source", "id"])
def test_peppol_requires_immutable_document_identity(field: str) -> None:
    document = {"source": "archive", "id": "document"}
    document[field] = ""
    with pytest.raises(ValueError, match=field):
        PeppolNode(document=document)


def test_peppol_rejects_application_destination_selection() -> None:
    with pytest.raises(ValueError, match="server configuration"):
        DocumentFlowRequest.fromdict(
            {
                "dag": {
                    "kind": "peppol",
                    "destination": "sandbox",
                    "document": {"source": "archive", "id": str(uuid4())},
                }
            }
        )
