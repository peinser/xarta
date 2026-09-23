from __future__ import annotations

from xarta.protocol.dag.archive import ArchiveNode
from xarta.protocol.document.request.flow import DocumentFlowRequest

from .archive_representations import flow


def test_flow_exercises_distinct_archive_representations() -> None:
    definition, ids = flow(7, 3)

    request = DocumentFlowRequest.fromdict(definition)

    assert isinstance(request.dag, ArchiveNode)
    assert len(request.dag.documents) == 1
    document = request.dag.documents[0]
    assert len(document.representations) == 3
    assert (
        document.default_representation_id
        == document.representations[0].representation_id
    )
    assert {str(item.representation_id) for item in document.representations} == {
        item["representation"] for item in ids["representations"]
    }
    assert {str(item.source.id) for item in document.representations} == set(
        definition["files"]
    )
