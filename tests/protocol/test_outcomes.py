from __future__ import annotations

from uuid import uuid4

import pytest

import xarta.protocol.dag

from xarta.protocol.dag import COMMON_OUTCOMES
from xarta.protocol.dag import Node
from xarta.protocol.dag import NodeTask
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import resolve_next_nodes
from xarta.protocol.dag import supported_node_kinds
from xarta.protocol.dag import walk_nodes


def test_on_round_trip_assigns_parents_and_terminal_state() -> None:
    root = xarta.protocol.dag.parse(
        {
            "kind": "debug",
            "on": {
                "success": [{"kind": "debug", "on": {"failure": []}}],
            },
        }
    )

    restored = xarta.protocol.dag.parse(root.dict())

    assert restored.on is not None
    child = restored.on["success"][0]
    assert child.parent is restored
    assert restored.terminal() is False
    assert child.terminal() is True


def test_node_internal_model_has_only_unified_edges() -> None:
    node = Node(kind="debug", on={"success": []})

    assert not hasattr(node, "on_success")
    assert not hasattr(node, "on_failure")
    assert not hasattr(node, "on_outcome")


@pytest.mark.parametrize("legacy", ["on_success", "on_failure", "on_outcome"])
def test_parser_rejects_legacy_edges(legacy) -> None:
    with pytest.raises(ValueError, match="Legacy DAG edge"):
        xarta.protocol.dag.parse({"kind": "debug", legacy: []})


def test_unknown_capability_outcome_is_rejected() -> None:
    with pytest.raises(ValueError, match="mailox_full"):
        xarta.protocol.dag.parse(
            {
                "kind": "email",
                "to": "a@example.com",
                "sender": "s@example.com",
                "body": {"plain": "test"},
                "on": {"mailox_full": []},
            }
        )


@pytest.mark.parametrize("outcome", sorted(COMMON_OUTCOMES))
def test_common_runtime_outcomes_are_valid_for_every_node(outcome) -> None:
    for specification in (
        {"kind": "debug"},
        {"kind": "generate", "documents": []},
        {"kind": "archive", "documents": []},
        {"kind": "email", "to": "a@example.com", "sender": "s@example.com", "body": {}},
    ):
        specification["on"] = {outcome: []}
        assert xarta.protocol.dag.parse(specification).on == {outcome: []}


def test_routing_uses_only_exact_outcome_name() -> None:
    successor = Node(kind="debug")
    node = Node(kind="debug", on={"success": [successor], "failure": []})
    emission = OutcomeEmission(
        outcome="success",
        details={"smtp_code": 550, "provider_status": "failure"},
    )

    assert resolve_next_nodes(node, emission.outcome) == [successor]
    assert resolve_next_nodes(node, "missing") == []
    assert resolve_next_nodes(node, "failure") == []


def test_node_task_round_trip_separates_node_and_execution_identity() -> None:
    node = Node(kind="debug", id=uuid4())
    task = NodeTask(
        flow_id=uuid4(),
        node=node,
        admission={
            "type": "x402",
            "network": "eip155:84532",
            "transaction": "0x" + "a" * 64,
        },
    )

    restored = NodeTask.fromdict(task.dict())

    assert restored.node.id == node.id
    assert restored.node_execution_id == task.node_execution_id
    assert restored.node_execution_id != node.id
    assert restored.admission == task.admission


def test_walk_nodes_visits_each_node_in_declaration_order() -> None:
    root = xarta.protocol.dag.parse(
        {
            "kind": "debug",
            "on": {
                "success": [
                    {"kind": "debug"},
                    {"kind": "debug", "on": {"failure": [{"kind": "debug"}]}},
                ]
            },
        }
    )

    assert [node.kind for node in walk_nodes(root)] == [
        "debug",
        "debug",
        "debug",
        "debug",
    ]


def test_supported_node_kinds_matches_protocol_parsers() -> None:
    assert supported_node_kinds() == frozenset(
        {
            "archive",
            "bundle",
            "debug",
            "doccle",
            "email",
            "generate",
            "peppol",
            "postal",
            "sftp",
            "search-index",
            "signature",
            "transform",
            "wait-for",
            "webhook",
        }
    )
