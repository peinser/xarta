from __future__ import annotations

from dataclasses import dataclass

import orjson

from xarta.protocol.dag import Node
from xarta.protocol.dag import node_contract
from xarta.protocol.dag import supported_node_kinds
from xarta.protocol.dag import walk_nodes
from xarta.protocol.dag.contracts import flow_schema


class UnsupportedCapabilitiesError(ValueError):
    """Raised when an intake DAG requires capabilities not deployed here."""

    def __init__(self, kinds: frozenset[str]) -> None:
        self.kinds = kinds
        super().__init__(
            f"DAG requires unavailable capabilities: {', '.join(sorted(kinds))}"
        )


@dataclass(frozen=True)
class CapabilityManifest:
    """Executable DAG capabilities advertised by this deployment."""

    kinds: frozenset[str]

    def __post_init__(self) -> None:
        if not all(isinstance(kind, str) and kind for kind in self.kinds):
            raise ValueError("Capability names must be non-empty strings")

        unknown = self.kinds - supported_node_kinds()
        if unknown:
            raise ValueError(
                "Capability manifest contains unknown node kinds: "
                f"{', '.join(sorted(unknown))}"
            )

    @classmethod
    def from_json(cls, value: str) -> CapabilityManifest:
        try:
            payload = orjson.loads(value)
        except orjson.JSONDecodeError as ex:
            raise ValueError("INTAKE_CAPABILITIES must contain valid JSON") from ex

        if not isinstance(payload, list) or not all(
            isinstance(kind, str) and kind for kind in payload
        ):
            raise ValueError("INTAKE_CAPABILITIES must be a JSON array of strings")

        return cls(kinds=frozenset(payload))

    def require_supported(self, root: Node) -> None:
        required = frozenset(node.kind for node in walk_nodes(root))
        unsupported = required - self.kinds
        if unsupported:
            raise UnsupportedCapabilitiesError(unsupported)

    def contracts(self) -> list[dict]:
        """Return graph construction contracts for deployed capabilities."""
        return [node_contract(kind) for kind in sorted(self.kinds)]

    def schema(self) -> dict:
        """Return a recursive JSON Schema restricted to deployed capabilities."""
        return flow_schema(self.contracts())
