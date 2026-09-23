from __future__ import annotations

import copy
import hashlib

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

import orjson

from xarta.crypto.sign.policy import ELECTRONIC_SEAL_V1
from xarta.crypto.sign.policy import SignatureError
from xarta.crypto.sign.policy import SignaturePolicyRegistry
from xarta.crypto.sign.policy import validate_policy_id
from xarta.exceptions.protocol import UnknownDAGNode
from xarta.flow_profiles import FlowProfileError
from xarta.flow_profiles import FlowProfileReference
from xarta.flow_profiles import FlowProfileRegistry
from xarta.flow_profiles import compile_flow_profile
from xarta.protocol.dag import walk_nodes
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.document.request.flow import DocumentFlowRequest
from xarta.protocol.document.source import DocumentSourceResult

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .capabilities import CapabilityManifest


class IntakeValidationError(ValueError):
    """Raised when an intake request cannot be prepared safely."""


@dataclass(frozen=True)
class IntakeUpload:
    """An uploaded document before it is persisted to temporary storage."""

    name: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class PreparedIntake:
    """A validated intake request that has not caused external side effects."""

    flow: DocumentFlowRequest
    sources: tuple[DocumentSourceResult, ...]
    semantic_fingerprint: str
    flow_id_supplied: bool
    profile_reference: FlowProfileReference | None = None
    profile_fingerprint: str | None = None

    def staging_dict(self) -> dict:
        result = self.flow.dict()
        result["semantic_fingerprint"] = self.semantic_fingerprint
        if self.profile_reference is not None:
            result["delivery_profile"] = str(self.profile_reference)
            result["profile_fingerprint"] = self.profile_fingerprint
        return result


def prepare_intake(
    flow_definition: dict | bytes,
    *,
    uploads: Iterable[IntakeUpload] = (),
    capabilities: CapabilityManifest,
    profiles: FlowProfileRegistry | None = None,
    default_signature_policy: str = ELECTRONIC_SEAL_V1.id,
    signature_policies: SignaturePolicyRegistry | None = None,
    allow_development_policies: bool = True,
) -> PreparedIntake:
    """Parse and validate an intake request without performing any I/O."""
    if isinstance(flow_definition, bytes):
        try:
            flow_definition = orjson.loads(flow_definition)
        except orjson.JSONDecodeError as ex:
            raise IntakeValidationError(
                "The flow definition must contain valid JSON."
            ) from ex

    if not isinstance(flow_definition, dict):
        raise IntakeValidationError("A JSON flow definition is required.")

    flow_definition = copy.deepcopy(flow_definition)

    for field in ("id", "correlation_id"):
        value = flow_definition.get(field)
        if value is not None and not isinstance(value, str):
            raise IntakeValidationError(f"The flow {field} must be a UUID string.")

    supplied_flow_id = flow_definition.get("id")
    if supplied_flow_id:
        try:
            flow_id = UUID(supplied_flow_id)
        except ValueError as ex:
            raise IntakeValidationError("The flow id must be a UUID string.") from ex
        if not flow_definition.get("correlation_id"):
            flow_definition["correlation_id"] = str(uuid5(flow_id, "correlation-id"))
    else:
        flow_id = uuid4()
        flow_definition["id"] = str(flow_id)

    profile_reference = None
    profile_fingerprint = None
    has_inline_dag = "dag" in flow_definition
    has_profile = "delivery_profile" in flow_definition
    if has_inline_dag == has_profile:
        raise IntakeValidationError("Provide exactly one of dag or delivery_profile.")
    if not has_profile and "delivery_values" in flow_definition:
        raise IntakeValidationError("delivery_values requires delivery_profile.")
    if has_profile:
        try:
            profile_reference = FlowProfileReference.parse(
                flow_definition["delivery_profile"]
            )
            profile = (profiles or FlowProfileRegistry.empty()).resolve(
                profile_reference
            )
            dag = compile_flow_profile(
                profile,
                flow_id=flow_id,
                values=flow_definition.get("delivery_values", {}),
            )
            profile_fingerprint = profile.fingerprint
        except FlowProfileError as ex:
            raise IntakeValidationError(str(ex)) from ex
    else:
        if supplied_flow_id:
            _assign_deterministic_node_ids(flow_definition.get("dag"), flow_id, "root")
        dag = None

    file_details = flow_definition.get("files", {})
    if not isinstance(file_details, dict):
        raise IntakeValidationError("The flow files field must be an object.")

    sources = []
    for upload in uploads:
        details = _file_details(file_details, upload.name)
        sources.append(
            DocumentSourceResult.parse(
                id=_upload_identifier(upload.name),
                content_type=upload.content_type,
                data=upload.body,
                document_type=details.get("document_type"),
                metadata=details.get("metadata"),
            )
        )

    try:
        flow = (
            DocumentFlowRequest(
                id=flow_id,
                correlation_id=(
                    UUID(flow_definition["correlation_id"])
                    if flow_definition.get("correlation_id")
                    else None
                ),
                dag=dag,
            )
            if dag is not None
            else DocumentFlowRequest.fromdict(data=flow_definition)
        )
    except KeyError as ex:
        raise IntakeValidationError(
            f"The flow definition is missing required field: {ex.args[0]}"
        ) from ex
    except (TypeError, UnknownDAGNode, ValueError) as ex:
        raise IntakeValidationError(f"The flow definition is invalid: {ex}") from ex

    try:
        bind_signature_policy_defaults(
            flow.dag,
            default_policy=default_signature_policy,
        )
        if signature_policies is not None:
            for candidate in walk_nodes(flow.dag):
                if isinstance(candidate, SignatureNode):
                    signature_policies.resolve(
                        candidate.policy,
                        default_policy=default_signature_policy,
                        allow_development_policies=allow_development_policies,
                    )
    except (SignatureError, ValueError) as ex:
        raise IntakeValidationError(str(ex)) from ex

    capabilities.require_supported(flow.dag)
    semantic_fingerprint = _semantic_fingerprint(flow, tuple(sources))
    return PreparedIntake(
        flow=flow,
        sources=tuple(sources),
        semantic_fingerprint=semantic_fingerprint,
        flow_id_supplied=bool(supplied_flow_id),
        profile_reference=profile_reference,
        profile_fingerprint=profile_fingerprint,
    )


def bind_signature_policy_defaults(
    node,
    *,
    default_policy: str,
) -> None:
    """Pin omitted signature policies before the DAG becomes durable."""
    validate_policy_id(default_policy)
    for candidate in walk_nodes(node):
        if isinstance(candidate, SignatureNode) and candidate.policy is None:
            candidate.policy = default_policy


def _semantic_fingerprint(
    flow: DocumentFlowRequest, sources: tuple[DocumentSourceResult, ...]
) -> str:
    source_semantics = [
        {
            "id": str(source.id),
            "content_type": source.content_type,
            "document_type": source.document_type.value,
            "metadata": source.metadata,
            "sha256": hashlib.sha256(source.data).hexdigest(),
            "size": len(source.data),
        }
        for source in sorted(sources, key=lambda item: item.id)
    ]
    canonical = orjson.dumps(
        {
            "flow_id": str(flow.id),
            "dag": flow.dag.dict(),
            "sources": source_semantics,
        },
        default=str,
        option=orjson.OPT_SORT_KEYS,
    )
    return (
        "sha256:"
        + hashlib.sha256(b"xarta-intake-semantics-v1\0" + canonical).hexdigest()
    )


def _assign_deterministic_node_ids(node: object, flow_id: UUID, path: str) -> None:
    if not isinstance(node, dict):
        return
    if not node.get("id"):
        node["id"] = str(uuid5(flow_id, f"node:{path}"))
    transitions = node.get("on")
    if not isinstance(transitions, dict):
        return
    for outcome, successors in transitions.items():
        if not isinstance(successors, list):
            continue
        for index, successor in enumerate(successors):
            _assign_deterministic_node_ids(
                successor, flow_id, f"{path}/{outcome}/{index}"
            )


def _upload_identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as ex:
        raise IntakeValidationError("Uploaded document names must be UUIDs.") from ex


def _file_details(files: dict, name: str) -> dict:
    details = files.get(name, {})
    if not isinstance(details, dict):
        raise IntakeValidationError(f"File details for {name} must be an object.")

    document_type = details.get("document_type")
    if document_type is not None and not isinstance(document_type, str):
        raise IntakeValidationError(f"File document_type for {name} must be a string.")

    metadata = details.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise IntakeValidationError(f"File metadata for {name} must be an object.")
    return details
