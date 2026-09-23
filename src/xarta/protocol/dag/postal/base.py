from __future__ import annotations

import re

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from uuid import UUID

import orjson

import xarta.protocol.dag

from xarta.protocol.dag import Node

if TYPE_CHECKING:
    from xarta.protocol.document.source import DocumentSource


MAX_POSTAL_DOCUMENTS = 25
MAX_POSTAL_REFERENCES = 32
MAX_POSTAL_EXTENSIONS = 32
MAX_POSTAL_EXTENSIONS_BYTES = 16 * 1024

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REFERENCE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_EXTENSION_KEY = re.compile(r"^[a-z][a-z0-9.-]{0,62}:[a-z][a-z0-9._-]{0,62}$")
_BELGIAN_POSTAL_CODE = re.compile(r"^[1-9][0-9]{3}$")


def _require_fields(data: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(
            f"{context} contains unsupported fields: {', '.join(sorted(unknown))}"
        )


def _required_text(value: Any, context: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    if len(value) > maximum:
        raise ValueError(f"{context} exceeds its length limit")
    return value


def _optional_text(value: Any, context: str, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _required_text(value, context, maximum)


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be an object")
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class PostalOutcome(StrEnum):
    PREPARED = "prepared"
    HANDED_OVER = "handed_over"
    CARRIER_ACCEPTED = "carrier_accepted"
    OUT_FOR_DELIVERY = "out_for_delivery"
    AVAILABLE_FOR_PICKUP = "available_for_pickup"
    DELIVERY_EXCEPTION = "delivery_exception"
    DELIVERED = "delivered"
    RETURNED = "returned"
    DEPOSIT_EVIDENCE_AVAILABLE = "deposit_evidence_available"
    DELIVERY_EVIDENCE_AVAILABLE = "delivery_evidence_available"
    INVALID_ADDRESS = "invalid_address"
    UNSUPPORTED_SERVICE = "unsupported_service"
    CAPACITY_REJECTED = "capacity_rejected"
    PRODUCTION_FAILED = "production_failed"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    CANCELLED = "cancelled"


class OrdinaryLetterSpeed(StrEnum):
    PRIORITY = "priority"
    NON_PRIORITY = "non_priority"


class RegisteredAcknowledgement(StrEnum):
    NONE = "none"


class PrintColorMode(StrEnum):
    MONOCHROME = "monochrome"
    COLOR = "color"


class PrintSides(StrEnum):
    SIMPLEX = "simplex"
    DUPLEX_LONG_EDGE = "duplex_long_edge"
    DUPLEX_SHORT_EDGE = "duplex_short_edge"


class DocumentBoundaryPolicy(StrEnum):
    CONTINUOUS = "continuous"
    START_ON_NEW_SHEET = "start_on_new_sheet"
    START_ON_RECTO = "start_on_recto"


@dataclass(frozen=True, slots=True)
class PostalDocumentReference:
    source: DocumentSource
    specification: Mapping[str, Any]
    role: str | None = None

    @classmethod
    def fromdict(cls, value: Any) -> PostalDocumentReference:
        data = dict(_object(value, "Postal document"))
        source_name = data.get("source")
        if not isinstance(source_name, str):
            raise ValueError(
                "Postal document source must be generate, render, or archive"
            )
        allowed_by_source = {
            "generate": {"source", "id", "role"},
            "archive": {"source", "id", "archive", "version", "role"},
            "render": {
                "source",
                "id",
                "document_type",
                "payload",
                "template_engine",
                "template_engines_options",
                "parameters",
                "content_type",
                "metadata",
                "role",
            },
        }
        allowed = allowed_by_source.get(source_name)
        if allowed is None:
            raise ValueError(
                "Postal document source must be generate, render, or archive"
            )
        _require_fields(data, allowed, "Postal document")
        identifier = _required_text(data.get("id"), "Postal document id", 128)
        try:
            parsed_identifier = UUID(identifier)
        except ValueError as error:
            raise ValueError("Postal document id must be a UUID") from error
        role = _optional_text(data.pop("role", None), "Postal document role", 64)
        from xarta.protocol.document import source

        parsed = source.parse(**{**data, "id": parsed_identifier})
        return cls(parsed, _freeze_json(data), role)

    def dict(self) -> dict[str, Any]:
        value = cast(dict[str, Any], _thaw_json(self.specification))
        if self.role is not None:
            value["role"] = self.role
        return value


@dataclass(frozen=True, slots=True)
class LetterContent:
    documents: tuple[PostalDocumentReference, ...]

    @classmethod
    def fromdict(cls, value: Any) -> LetterContent:
        data = _object(value, "Postal letter content")
        _require_fields(data, {"documents"}, "Postal letter content")
        documents = data.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError("Postal letter content requires at least one document")
        if len(documents) > MAX_POSTAL_DOCUMENTS:
            raise ValueError("Postal letter content has too many documents")
        return cls(tuple(PostalDocumentReference.fromdict(item) for item in documents))

    def dict(self) -> dict[str, Any]:
        return {"documents": [document.dict() for document in self.documents]}


@dataclass(frozen=True, slots=True)
class BelgianStructuredAddress:
    street: str
    house_number: str
    postal_code: str
    city: str
    box_number: str | None = None
    region: str | None = None
    format: str = "structured"
    country_code: str = "BE"

    @classmethod
    def fromdict(cls, value: Any) -> BelgianStructuredAddress:
        data = _object(value, "Postal recipient address")
        _require_fields(
            data,
            {
                "format",
                "street",
                "house_number",
                "box_number",
                "postal_code",
                "city",
                "region",
                "country_code",
            },
            "Postal recipient address",
        )
        if data.get("format") != "structured":
            raise ValueError("Postal recipient address format must be structured")
        if data.get("country_code") != "BE":
            raise ValueError("Postal recipient country_code must be BE")
        postal_code = _required_text(
            data.get("postal_code"), "Postal recipient postal_code", 4
        )
        if not _BELGIAN_POSTAL_CODE.fullmatch(postal_code):
            raise ValueError("Postal recipient postal_code must be a Belgian postcode")
        return cls(
            street=_required_text(data.get("street"), "Postal recipient street", 128),
            house_number=_required_text(
                data.get("house_number"), "Postal recipient house_number", 16
            ),
            box_number=_optional_text(
                data.get("box_number"), "Postal recipient box_number", 16
            ),
            postal_code=postal_code,
            city=_required_text(data.get("city"), "Postal recipient city", 128),
            region=_optional_text(data.get("region"), "Postal recipient region", 128),
        )

    def dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "street": self.street,
            "house_number": self.house_number,
            "box_number": self.box_number,
            "postal_code": self.postal_code,
            "city": self.city,
            "region": self.region,
            "country_code": self.country_code,
        }


@dataclass(frozen=True, slots=True)
class PostalRecipient:
    address: BelgianStructuredAddress
    name: str | None = None
    company_name: str | None = None
    department: str | None = None

    @classmethod
    def fromdict(cls, value: Any) -> PostalRecipient:
        data = _object(value, "Postal recipient")
        _require_fields(
            data, {"name", "company_name", "department", "address"}, "Postal recipient"
        )
        name = _optional_text(data.get("name"), "Postal recipient name", 128)
        company_name = _optional_text(
            data.get("company_name"), "Postal recipient company_name", 128
        )
        if name is None and company_name is None:
            raise ValueError("Postal recipient requires name or company_name")
        return cls(
            name=name,
            company_name=company_name,
            department=_optional_text(
                data.get("department"), "Postal recipient department", 128
            ),
            address=BelgianStructuredAddress.fromdict(data.get("address")),
        )

    def dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"address": self.address.dict()}
        if self.name is not None:
            value["name"] = self.name
        if self.company_name is not None:
            value["company_name"] = self.company_name
        if self.department is not None:
            value["department"] = self.department
        return value


@dataclass(frozen=True, slots=True)
class SenderProfile:
    id: str
    type: str = "profile"

    @classmethod
    def fromdict(cls, value: Any) -> SenderProfile:
        data = _object(value, "Postal sender")
        _require_fields(data, {"type", "id"}, "Postal sender")
        if data.get("type") != "profile":
            raise ValueError("Postal sender type must be profile")
        identifier = _required_text(data.get("id"), "Postal sender profile id", 128)
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("Postal sender profile id is invalid")
        return cls(identifier)

    def dict(self) -> dict[str, str]:
        return {"type": self.type, "id": self.id}


@dataclass(frozen=True, slots=True)
class OrdinaryLetterServiceV1:
    speed: OrdinaryLetterSpeed
    type: str = "ordinary"
    version: int = 1

    def dict(self) -> dict[str, Any]:
        return {"type": self.type, "version": self.version, "speed": self.speed.value}


@dataclass(frozen=True, slots=True)
class RegisteredLetterServiceV1:
    acknowledgement: RegisteredAcknowledgement = RegisteredAcknowledgement.NONE
    type: str = "registered"
    version: int = 1

    def dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "version": self.version,
            "acknowledgement": self.acknowledgement.value,
        }


LetterServiceV1 = OrdinaryLetterServiceV1 | RegisteredLetterServiceV1


def _parse_service(value: Any) -> LetterServiceV1:
    data = _object(value, "Postal service")
    service_type = data.get("type")
    if service_type == "ordinary":
        _require_fields(data, {"type", "version", "speed"}, "Postal service")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Postal ordinary service version must be 1")
        try:
            speed = data.get("speed")
            if not isinstance(speed, str):
                raise ValueError
            return OrdinaryLetterServiceV1(OrdinaryLetterSpeed(speed))
        except (TypeError, ValueError) as error:
            raise ValueError("Postal ordinary service speed is unsupported") from error
    if service_type == "registered":
        _require_fields(data, {"type", "version", "acknowledgement"}, "Postal service")
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Postal registered service version must be 1")
        try:
            raw_acknowledgement = data.get("acknowledgement")
            if not isinstance(raw_acknowledgement, str):
                raise ValueError
            acknowledgement = RegisteredAcknowledgement(raw_acknowledgement)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Postal registered service acknowledgement is unsupported"
            ) from error
        return RegisteredLetterServiceV1(acknowledgement)
    raise ValueError("Postal service type must be ordinary or registered")


@dataclass(frozen=True, slots=True)
class LetterPrintSettingsV1:
    color_mode: PrintColorMode
    sides: PrintSides
    document_boundary: DocumentBoundaryPolicy
    version: int = 1

    @classmethod
    def fromdict(cls, value: Any) -> LetterPrintSettingsV1:
        data = _object(value, "Postal print settings")
        _require_fields(
            data,
            {"version", "color_mode", "sides", "document_boundary"},
            "Postal print settings",
        )
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Postal print settings version must be 1")
        try:
            color_mode = data.get("color_mode")
            sides = data.get("sides")
            document_boundary = data.get("document_boundary")
            if (
                not isinstance(color_mode, str)
                or not isinstance(sides, str)
                or not isinstance(document_boundary, str)
            ):
                raise ValueError
            return cls(
                PrintColorMode(color_mode),
                PrintSides(sides),
                DocumentBoundaryPolicy(document_boundary),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Postal print settings contain an unsupported value"
            ) from error

    def dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "color_mode": self.color_mode.value,
            "sides": self.sides.value,
            "document_boundary": self.document_boundary.value,
        }


def _parse_references(value: Any) -> Mapping[str, str]:
    data = _object(value, "Postal references")
    if len(data) > MAX_POSTAL_REFERENCES:
        raise ValueError("Postal references have too many entries")
    references = {}
    for key, item in data.items():
        if not isinstance(key, str) or not _REFERENCE_KEY.fullmatch(key):
            raise ValueError("Postal reference key is invalid")
        references[key] = _required_text(item, f"Postal reference {key}", 256)
    return MappingProxyType(references)


def _parse_extensions(value: Any) -> Mapping[str, Any]:
    data = _object(value, "Postal extensions")
    if len(data) > MAX_POSTAL_EXTENSIONS:
        raise ValueError("Postal extensions have too many entries")
    if any(
        not isinstance(key, str) or not _EXTENSION_KEY.fullmatch(key) for key in data
    ):
        raise ValueError("Postal extension keys must be namespaced")
    try:
        encoded = orjson.dumps(data)
    except (TypeError, ValueError) as error:
        raise ValueError("Postal extensions must be JSON serializable") from error
    if len(encoded) > MAX_POSTAL_EXTENSIONS_BYTES:
        raise ValueError("Postal extensions exceed the size limit")
    # JSON round-tripping creates a private, recursively detached value tree.
    return cast(Mapping[str, Any], _freeze_json(orjson.loads(encoded)))


@dataclass(frozen=True, slots=True)
class LetterV1:
    content: LetterContent
    recipient: PostalRecipient
    sender: SenderProfile
    service: LetterServiceV1
    print: LetterPrintSettingsV1 | None = None
    references: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    extensions: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    type: str = "letter"
    version: int = 1

    @classmethod
    def fromdict(cls, value: Any) -> LetterV1:
        data = _object(value, "Postal mailpiece")
        _require_fields(
            data,
            {
                "type",
                "version",
                "content",
                "recipient",
                "sender",
                "service",
                "print",
                "references",
                "extensions",
            },
            "Postal mailpiece",
        )
        if (
            data.get("type") != "letter"
            or type(data.get("version")) is not int
            or data["version"] != 1
        ):
            raise ValueError("Postal mailpiece must be letter version 1")
        print_settings = data.get("print")
        return cls(
            content=LetterContent.fromdict(data.get("content")),
            recipient=PostalRecipient.fromdict(data.get("recipient")),
            sender=SenderProfile.fromdict(data.get("sender")),
            service=_parse_service(data.get("service")),
            print=(
                LetterPrintSettingsV1.fromdict(print_settings)
                if print_settings is not None
                else None
            ),
            references=_parse_references(data.get("references", {})),
            extensions=_parse_extensions(data.get("extensions", {})),
        )

    def dict(self) -> dict[str, Any]:
        value = {
            "type": self.type,
            "version": self.version,
            "content": self.content.dict(),
            "recipient": self.recipient.dict(),
            "sender": self.sender.dict(),
            "service": self.service.dict(),
            "references": dict(self.references),
            "extensions": _thaw_json(self.extensions),
        }
        if self.print is not None:
            value["print"] = self.print.dict()
        return value


@dataclass(frozen=True, slots=True)
class PostalRequest:
    destination: str | None
    mailpiece: LetterV1


class PostalNode(Node):
    KIND = "postal"
    OUTCOMES = frozenset(outcome.value for outcome in PostalOutcome)

    def __init__(
        self,
        mailpiece: LetterV1 | Mapping[str, Any],
        destination: str | None = None,
        id: UUID | None = None,
        on: dict[str, list[Node]] | None = None,
        parent: Node | None = None,
    ) -> None:
        if destination is not None:
            destination = _required_text(destination, "Postal destination", 128)
        super().__init__(kind=self.KIND, id=id, on=on, parent=parent)
        self.destination = destination
        self.mailpiece = (
            mailpiece
            if isinstance(mailpiece, LetterV1)
            else LetterV1.fromdict(mailpiece)
        )

    def interpret(self) -> PostalRequest:
        return PostalRequest(self.destination, self.mailpiece)

    def dict(self) -> dict[str, Any]:
        specification = super().dict()
        specification.update(
            {"destination": self.destination, "mailpiece": self.mailpiece.dict()}
        )
        return specification

    @staticmethod
    def fromdict(
        _specification: Mapping[str, Any],
        mailpiece: Mapping[str, Any],
        destination: str | None = None,
        **kwargs: Any,
    ) -> PostalNode:
        _require_fields(
            _specification,
            {"kind", "id", "destination", "mailpiece", "on", "parent"},
            "Postal node",
        )
        return PostalNode(
            **xarta.protocol.dag.parse_common_fields(**dict(_specification)),
            destination=destination,
            mailpiece=mailpiece,
        )
