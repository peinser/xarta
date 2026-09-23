from __future__ import annotations

from collections import defaultdict
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from xarta.protocol.dag import COMMON_OUTCOMES
from xarta.protocol.dag import Node
from xarta.protocol.dag.email import EmailNode
from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.dag.postal import PostalNode


class UnpriceableGraphError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphPricingLimits:
    maximum_static_nodes: int = 1_000
    maximum_activations_per_node: int = 100_000
    maximum_total_activations: int = 1_000_000


_EMAIL_RECIPIENT_OUTCOMES = frozenset(
    {
        "accepted",
        "delivered",
        "mailbox_full",
        "recipient_unknown",
        "message_rejected",
        "delivery_delayed",
        "complained",
    }
)
_EMAIL_AGGREGATE_OUTCOMES = frozenset(
    {
        "all_delivered",
        "all_accepted",
        "partially_accepted",
        "partially_delivered",
        "all_failed",
    }
)


def maximum_outcome_emissions(node: Node, outcome: str) -> int:
    if outcome in COMMON_OUTCOMES:
        return 1
    if isinstance(node, EmailNode):
        if outcome in _EMAIL_RECIPIENT_OUTCOMES:
            return 1 + len(node.cc) + len(node.bcc)
        if outcome in _EMAIL_AGGREGATE_OUTCOMES:
            return 1
        raise UnpriceableGraphError(f"Unknown email outcome cardinality: {outcome}")
    if isinstance(node, PeppolNode):
        if outcome in node.OUTCOMES:
            return 1
        raise UnpriceableGraphError(f"Unknown Peppol outcome cardinality: {outcome}")
    if isinstance(node, PostalNode):
        if outcome in node.OUTCOMES:
            return 1
        raise UnpriceableGraphError(f"Unknown postal outcome cardinality: {outcome}")
    if node.kind == "archive":
        if outcome in {
            "created",
            "version_created",
            "unchanged",
            "overwrite_rejected",
            "version_conflict",
        }:
            return len(getattr(node, "documents", ()))
        if outcome in {"success", "failure"}:
            return 1
        raise UnpriceableGraphError(f"Unknown archive outcome cardinality: {outcome}")
    explicitly_single = {
        "bundle",
        "doccle",
        "generate",
        "search-index",
        "sftp",
        "signature",
        "transform",
        "wait-for",
        "webhook",
    }
    if node.kind in explicitly_single and outcome in node.OUTCOMES:
        return 1
    raise UnpriceableGraphError(
        f"No explicit outcome cardinality for {node.kind}:{outcome}"
    )


def canonical_node(node: Node):
    def normalize(value):
        if isinstance(value, Node):
            return ("node", value.id)
        if isinstance(value, Mapping):
            return tuple(
                (key, normalize(item))
                for key, item in sorted(value.items(), key=lambda pair: pair[0])
            )
        if isinstance(value, (list, tuple)):
            return tuple(normalize(item) for item in value)
        return value

    return normalize(vars(node))


def collect_static_nodes(
    root: Node, limits: GraphPricingLimits = GraphPricingLimits()
) -> dict[UUID, Node]:
    nodes: dict[UUID, Node] = {}
    pending = [root]
    while pending:
        node = pending.pop()
        if node.id is None:
            raise UnpriceableGraphError("Every priced node requires an ID")
        existing = nodes.get(node.id)
        if existing is not None:
            if canonical_node(existing) != canonical_node(node):
                raise UnpriceableGraphError(
                    "One Node.id has conflicting specifications"
                )
            continue
        nodes[node.id] = node
        if len(nodes) > limits.maximum_static_nodes:
            raise UnpriceableGraphError("Static node pricing limit exceeded")
        pending.extend(
            successor
            for successors in (node.on or {}).values()
            for successor in successors
        )
    return nodes


def maximum_activation_bounds(
    root: Node,
    limits: GraphPricingLimits = GraphPricingLimits(),
    *,
    nodes: dict[UUID, Node] | None = None,
) -> dict[UUID, int]:
    nodes = nodes or collect_static_nodes(root, limits)
    incoming: dict[UUID, int] = defaultdict(int)
    edges: dict[UUID, list[tuple[UUID, int]]] = defaultdict(list)
    for node in nodes.values():
        if node.id is None:
            raise UnpriceableGraphError("Every priced node requires an ID")
        for outcome, successors in (node.on or {}).items():
            multiplier = maximum_outcome_emissions(node, outcome)
            for successor in successors:
                if successor.id is None:
                    raise UnpriceableGraphError("Every priced successor requires an ID")
                edges[node.id].append((successor.id, multiplier))
                incoming[successor.id] += 1

    ready = deque(node_id for node_id in nodes if incoming[node_id] == 0)
    activations: dict[UUID, int] = defaultdict(int)
    if root.id is None:
        raise UnpriceableGraphError("Root node requires an ID")
    activations[root.id] = 1
    visited = 0
    while ready:
        node_id = ready.popleft()
        visited += 1
        for successor_id, multiplier in edges[node_id]:
            contribution = activations[node_id] * multiplier
            activations[successor_id] += contribution
            if activations[successor_id] > limits.maximum_activations_per_node:
                raise UnpriceableGraphError(
                    "Per-node activation pricing limit exceeded"
                )
            incoming[successor_id] -= 1
            if incoming[successor_id] == 0:
                ready.append(successor_id)
    if visited != len(nodes):
        raise UnpriceableGraphError("DAG contains a cycle")
    if sum(activations.values()) > limits.maximum_total_activations:
        raise UnpriceableGraphError("Total activation pricing limit exceeded")
    return dict(activations)


def static_nodes(root: Node) -> dict[UUID, Node]:
    return collect_static_nodes(root)
