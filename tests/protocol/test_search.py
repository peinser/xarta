from __future__ import annotations

from uuid import uuid4

import pytest

from xarta.protocol.dag import parse
from xarta.protocol.dag.search import SearchIndexNode


def specification() -> dict:
    return {
        "kind": "search-index",
        "document": {
            "source": "archive",
            "id": str(uuid4()),
            "version": "3",
        },
        "destination": "documents",
        "on": {"indexed": [], "unchanged": [], "unsupported_content": []},
    }


def test_search_index_node_round_trip_and_outcomes() -> None:
    node = parse(specification())

    assert isinstance(node, SearchIndexNode)
    assert node.source_version == "3"
    assert parse(node.dict()).dict() == node.dict()


@pytest.mark.parametrize("missing", ["source", "id"])
def test_search_index_requires_immutable_source_identity(missing: str) -> None:
    value = specification()
    del value["document"][missing]

    with pytest.raises(ValueError, match=missing):
        parse(value)


def test_search_index_can_resolve_current_archive_version() -> None:
    value = specification()
    del value["document"]["version"]

    node = parse(value)

    assert isinstance(node, SearchIndexNode)
    assert node.source_version == "latest"


def test_search_index_rejects_multiple_document_shape() -> None:
    value = specification()
    value["documents"] = [value.pop("document")]

    with pytest.raises(TypeError, match="document"):
        parse(value)


def test_search_index_rejects_unknown_outcome() -> None:
    value = specification()
    value["on"] = {"queried": []}

    with pytest.raises(ValueError, match="Invalid outcome"):
        parse(value)
