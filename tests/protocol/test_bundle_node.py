from __future__ import annotations

import io
import zipfile

from uuid import uuid4

import pytest

import xarta.protocol.dag

from xarta.protocol.dag.bundle import BundleNode
from xarta.protocol.document.request import bundle as bundle_protocol
from xarta.services.v1.bundle.builder import build_zip


def bundle_specification() -> dict:
    return {
        "kind": "bundle",
        "documents": [
            {
                "source": "archive",
                "id": str(uuid4()),
                "archive": "default",
                "version": str(uuid4()),
                "filename": "invoice.pdf",
            }
        ],
        "out": str(uuid4()),
        "compression": {"method": "deflated", "level": 9},
    }


def test_bundle_node_round_trip_preserves_archive_source() -> None:
    specification = bundle_specification()

    node = xarta.protocol.dag.parse(specification)
    restored = xarta.protocol.dag.parse(node.dict())

    assert isinstance(node, BundleNode)
    assert isinstance(restored, BundleNode)
    assert restored.output_id == node.output_id
    assert restored.documents == node.documents
    interpreted = restored.interpret()
    assert interpreted[0].source.source == "archive"
    assert interpreted[0].filename == "invoice.pdf"


@pytest.mark.parametrize(
    "documents, message",
    [
        ([], "at least one"),
        (
            [
                {"source": "generate", "id": str(uuid4()), "filename": "same"},
                {"source": "generate", "id": str(uuid4()), "filename": "same"},
            ],
            "unique",
        ),
        (
            [{"source": "generate", "id": str(uuid4()), "filename": "../bad"}],
            "plain file names",
        ),
    ],
)
def test_bundle_node_rejects_invalid_members(documents, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        BundleNode(documents=documents, out=uuid4())


def test_zip_builder_is_deterministic() -> None:
    options = bundle_protocol.DocumentBundleRequestCompressionOptions.fromdict(
        {"method": "deflated", "level": 9}
    )
    members = [("second.txt", b"second"), ("first.txt", b"first")]

    first = build_zip(members, options)
    second = build_zip(reversed(members), options)

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["first.txt", "second.txt"]
        assert archive.read("first.txt") == b"first"
        assert archive.read("second.txt") == b"second"
        assert {entry.date_time for entry in archive.infolist()} == {
            (1980, 1, 1, 0, 0, 0)
        }
