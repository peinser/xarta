from __future__ import annotations

import uuid

from collections.abc import Iterable

from xarta.protocol.dag.node import Node
from xarta.protocol.dag.outcome import OutcomeEmission
from xarta.protocol.dag.outcome import OutcomeEvent
from xarta.protocol.dag.task import NodeTask
from xarta.protocol.dag.task import TriggerContext


def resolve_next_nodes(node: Node, outcome: str) -> list[Node]:
    if node.on is None:
        return []
    return node.on.get(outcome, [])


def resolve_synchronous_outcomes(
    task: NodeTask,
    outcomes: Iterable[OutcomeEmission],
    source: str,
) -> tuple[tuple[OutcomeEvent, ...], tuple[NodeTask, ...]]:
    """Build stable events and successors for a retried synchronous delivery."""
    events = []
    successors: dict[uuid.UUID, NodeTask] = {}
    for index, emission in enumerate(outcomes):
        event = OutcomeEvent(
            id=uuid.uuid5(task.node_execution_id, f"outcome:{index}"),
            flow_id=task.flow_id,
            node_execution_id=task.node_execution_id,
            outcome=emission.outcome,
            source=source,
            subject=emission.subject,
            details=emission.details,
            occurred_at=emission.occurred_at,
        )
        events.append(event)
        for successor in resolve_next_nodes(task.node, event.outcome):
            if successor.id is None:
                raise ValueError("A successor node must have a specification ID")
            execution_id = uuid.uuid5(event.id, str(successor.id))
            successors.setdefault(
                execution_id,
                NodeTask(
                    flow_id=task.flow_id,
                    node=successor,
                    node_execution_id=execution_id,
                    trigger=TriggerContext(
                        outcome_event_id=event.id,
                        outcome=event.outcome,
                        originating_node_execution_id=task.node_execution_id,
                        subject=event.subject,
                    ),
                    correlation_id=task.correlation_id,
                    admission=task.admission,
                    created_at=task.created_at,
                ),
            )
    return tuple(events), tuple(successors.values())
