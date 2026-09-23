from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from xarta.protocol.dag.sftp import SFTPNode
from xarta.protocol.document.request.flow import DocumentFlowRequest

EXAMPLES = Path(__file__).parents[2] / "examples" / "protocol"


def test_sftp_fixture_round_trip() -> None:
    payload = orjson.loads((EXAMPLES / "sftp-upload.json").read_bytes())
    request = DocumentFlowRequest.fromdict(payload)

    assert isinstance(request.dag, SFTPNode)
    assert request.dag.destination == "partner-sftp"
    assert request.dag.path == "invoices/2026/INV-2026-0042.pdf"
    assert DocumentFlowRequest.fromdict(request.dict()).dag.dict() == request.dag.dict()


@pytest.mark.parametrize(
    "path",
    ["", "/absolute/file.pdf", "../file.pdf", "folder/../file.pdf", "bad\x00name"],
)
def test_sftp_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="SFTP path"):
        SFTPNode(document={"source": "generate", "id": "unused"}, path=path)


def test_sftp_interpretation_is_repeatable() -> None:
    node = SFTPNode(
        document={
            "source": "generate",
            "id": "93000000-0000-0000-0000-000000000001",
        },
        path="invoices/invoice.pdf",
    )

    first, first_path = node.interpret()
    second, second_path = node.interpret()

    assert first.id == second.id
    assert first_path == second_path == "invoices/invoice.pdf"
