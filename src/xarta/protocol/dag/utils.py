r"""
Utilities asociated with the DAG document protocol.
"""

from __future__ import annotations

import inspect
import uuid

from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import cast

from xarta.exceptions.protocol import UnknownDAGNode
from xarta.protocol.dag.archive import ArchiveNode
from xarta.protocol.dag.bundle import BundleNode
from xarta.protocol.dag.debug import DebugNode
from xarta.protocol.dag.doccle import DoccleNode
from xarta.protocol.dag.email import EmailNode
from xarta.protocol.dag.generate import GenerateNode
from xarta.protocol.dag.node import Node
from xarta.protocol.dag.peppol import PeppolNode
from xarta.protocol.dag.postal import PostalNode
from xarta.protocol.dag.search import SearchIndexNode
from xarta.protocol.dag.sftp import SFTPNode
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.dag.transform import TransformNode
from xarta.protocol.dag.waitfor import WaitForNode
from xarta.protocol.dag.webhook import WebhookNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator


_NODE_TYPES: Final[dict[str, type[Node]]] = {
    ArchiveNode.KIND: ArchiveNode,
    BundleNode.KIND: BundleNode,
    DebugNode.KIND: DebugNode,
    DoccleNode.KIND: DoccleNode,
    EmailNode.KIND: EmailNode,
    GenerateNode.KIND: GenerateNode,
    PeppolNode.KIND: PeppolNode,
    PostalNode.KIND: PostalNode,
    SearchIndexNode.KIND: SearchIndexNode,
    SFTPNode.KIND: SFTPNode,
    SignatureNode.KIND: SignatureNode,
    TransformNode.KIND: TransformNode,
    WaitForNode.KIND: WaitForNode,
    WebhookNode.KIND: WebhookNode,
}

_NODE_PARSERS: Final[dict[str, Callable[..., Node]]] = {
    ArchiveNode.KIND: ArchiveNode.fromdict,
    BundleNode.KIND: BundleNode.fromdict,
    DebugNode.KIND: DebugNode.fromdict,
    DoccleNode.KIND: DoccleNode.fromdict,
    EmailNode.KIND: EmailNode.fromdict,
    GenerateNode.KIND: GenerateNode.fromdict,
    PeppolNode.KIND: PeppolNode.fromdict,
    PostalNode.KIND: PostalNode.fromdict,
    SearchIndexNode.KIND: SearchIndexNode.fromdict,
    SFTPNode.KIND: SFTPNode.fromdict,
    SignatureNode.KIND: SignatureNode.fromdict,
    TransformNode.KIND: TransformNode.fromdict,
    WaitForNode.KIND: WaitForNode.fromdict,
    WebhookNode.KIND: WebhookNode.fromdict,
}


def supported_node_kinds() -> frozenset[str]:
    """Return all node kinds understood by the document protocol."""
    return frozenset(_NODE_PARSERS)


def node_contract(kind: str) -> dict:
    """Describe the public graph fields and outcomes for one node kind."""
    node_type = _NODE_TYPES.get(kind)
    if node_type is None:
        raise ValueError(f"Unknown node kind: {kind}")
    parameters = inspect.signature(cast("Any", node_type).__init__).parameters.values()
    fields = {
        parameter.name: {
            "required": parameter.default is inspect.Parameter.empty,
            "type": (
                parameter.annotation
                if isinstance(parameter.annotation, str)
                else inspect.formatannotation(parameter.annotation)
            ),
        }
        for parameter in parameters
        if parameter.name not in {"self", "id", "on", "parent", "kwargs"}
        and parameter.kind is not inspect.Parameter.VAR_KEYWORD
    }
    from xarta.protocol.dag.contracts import capability_detail
    from xarta.protocol.dag.outcome import COMMON_OUTCOMES

    detail = capability_detail(kind)
    detail.pop("properties")
    detail.pop("required", None)
    return {
        "kind": kind,
        "schema_ref": f"#/$defs/node_{kind}",
        **detail,
        "fields": fields,
        "outcomes": sorted(node_type.OUTCOMES | COMMON_OUTCOMES),
    }


def walk_nodes(root: Node) -> Iterator[Node]:
    """Traverse a DAG once in deterministic, depth-first order."""
    pending = [root]
    visited: set[int] = set()

    while pending:
        node = pending.pop()
        identity = id(node)
        if identity in visited:
            continue
        visited.add(identity)
        yield node

        successors = [
            child for outcome in (node.on or {}).values() for child in outcome
        ]
        pending.extend(reversed(successors))


def parse(_specification: dict, parent: Node | None = None, **kwargs) -> Node:
    r"""
    Recursively parses the DAG based on the raw dictionary structure. The method
    will recursively determine the kind of the node and interpret its contents. Whenever
    an unknown node is detected, the method will raise an exception. It should be noted the graph is
    parsed in a bottom up approach. Errors will hence bubble up.
    """
    if not isinstance(_specification, dict):
        raise TypeError("DAG node specification must be an object")

    legacy_edges = {"on_success", "on_failure", "on_outcome"} & _specification.keys()
    if legacy_edges:
        fields = ", ".join(sorted(legacy_edges))
        raise ValueError(f"Legacy DAG edge fields are not supported: {fields}")
    raw_on = _specification.get("on")
    if raw_on is not None:
        if not isinstance(raw_on, dict):
            raise TypeError("on must be a mapping of outcomes to node lists")
        for outcome, successors in raw_on.items():
            if not isinstance(outcome, str):
                raise TypeError("Outcome names must be strings")
            if not isinstance(successors, list):
                raise TypeError(f"on[{outcome!r}] must be a list")

    kind = _specification.get("kind")
    if not isinstance(kind, str):
        raise UnknownDAGNode(str(kind))
    parser = _NODE_PARSERS.get(kind)
    if parser is None:
        raise UnknownDAGNode(kind)

    node = parser(_specification=_specification, **_specification, **kwargs)
    node.parent = parent
    for child in [
        child for successors in (node.on or {}).values() for child in successors
    ]:
        child.parent = node
    return node


def parse_common_fields(
    id: str | None = None,
    on: dict[str, list[dict]] | None = None,
    parent: Node | None = None,
    **kwargs,
) -> dict:
    r"""
    Parses the common fields of the task node. It should be noted the graph is
    parsed in a bottom up approach. Errors will hence bubble up.
    """
    return {
        "id": uuid.uuid4() if not id else uuid.UUID(str(id)),
        "on": (
            {
                outcome: [parse(node, parent=parent) for node in successors]
                for outcome, successors in on.items()
            }
            if on is not None
            else None
        ),
        "parent": parent,
    }
