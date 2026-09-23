"""Strict wire and package models used by the station."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID


class PrintColorMode(StrEnum):
    MONOCHROME = "monochrome"
    COLOR = "color"


class PrintSides(StrEnum):
    SIMPLEX = "simplex"
    DUPLEX_LONG_EDGE = "duplex_long_edge"
    DUPLEX_SHORT_EDGE = "duplex_short_edge"


class DocumentBoundary(StrEnum):
    CONTINUOUS = "continuous"
    START_ON_NEW_SHEET = "start_on_new_sheet"
    START_ON_RECTO = "start_on_recto"


class PrintJobKind(StrEnum):
    LETTER = "letter"


class PrintAttemptEvent(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ScanMode(StrEnum):
    START_PRODUCTION = "start_production"
    READY_FOR_HANDOVER = "ready_for_handover"
    CONFIRM_HANDOVER = "confirm_handover"


class ProductionStage(StrEnum):
    PROCESSING = "processing"
    PREPARED = "prepared"
    HANDED_OVER = "handed_over"


@dataclass(frozen=True, slots=True)
class PackageReceipt:
    package_sha256: str
    manifest_sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _require_sha256(self.package_sha256, "package_sha256")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if self.byte_count < 0:
            raise ValueError("byte_count must not be negative")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    ordinal: int
    path: str
    sha256: str
    byte_count: int
    page_count: int
    role: str

    @classmethod
    def from_dict(cls, value: object) -> SourceDocument:
        data = _object(value, "source")
        _exact_fields(
            data,
            {"ordinal", "path", "sha256", "byte_count", "page_count", "role"},
            "source",
        )
        source = cls(
            _integer(data["ordinal"], "source.ordinal"),
            _string(data["path"], "source.path"),
            _string(data["sha256"], "source.sha256"),
            _integer(data["byte_count"], "source.byte_count"),
            _integer(data["page_count"], "source.page_count"),
            _string(data["role"], "source.role"),
        )
        _require_sha256(source.sha256, "source.sha256")
        if source.ordinal < 1 or source.byte_count < 1 or source.page_count < 1:
            raise ValueError(
                "source ordinal, byte_count, and page_count must be positive"
            )
        return source


@dataclass(frozen=True, slots=True)
class PrintInstructions:
    color_mode: PrintColorMode
    sides: PrintSides
    document_boundary: DocumentBoundary

    @classmethod
    def from_dict(cls, value: object) -> PrintInstructions:
        data = _object(value, "print")
        _exact_fields(data, {"color_mode", "sides", "document_boundary"}, "print")
        return cls(
            _enum(PrintColorMode, data["color_mode"], "print.color_mode"),
            _enum(PrintSides, data["sides"], "print.sides"),
            _enum(
                DocumentBoundary, data["document_boundary"], "print.document_boundary"
            ),
        )


@dataclass(frozen=True, slots=True)
class TravellerInstructions:
    template_revision: str
    service: Mapping[str, Any]
    recipient: Mapping[str, Any]
    quantities: Mapping[str, Any]
    franking: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: object) -> TravellerInstructions:
        data = _object(value, "traveller")
        _exact_fields(
            data,
            {
                "schema",
                "template_revision",
                "service",
                "recipient",
                "quantities",
                "franking",
            },
            "traveller",
        )
        if data["schema"] != "xarta.postal.traveller/v1":
            raise ValueError("unsupported traveller schema")
        service = _object(data["service"], "traveller.service")
        _exact_fields(service, {"type", "speed"}, "traveller.service")
        _string(service["type"], "traveller.service.type")
        if service["speed"] is not None:
            _string(service["speed"], "traveller.service.speed")
        recipient = _object(data["recipient"], "traveller.recipient")
        _exact_fields(
            recipient, {"name", "company_name", "address"}, "traveller.recipient"
        )
        if recipient["name"] is not None:
            _string(recipient["name"], "traveller.recipient.name")
        if recipient["company_name"] is not None:
            _string(recipient["company_name"], "traveller.recipient.company_name")
        if recipient["name"] is None and recipient["company_name"] is None:
            raise ValueError("traveller recipient requires a name or company_name")
        address = _object(recipient["address"], "traveller.recipient.address")
        _exact_fields(
            address,
            {"street", "house_number", "postal_code", "city"},
            "traveller.recipient.address",
        )
        for name in ("street", "house_number", "postal_code", "city"):
            _string(address[name], f"traveller.recipient.address.{name}")
        quantities = _object(data["quantities"], "traveller.quantities")
        _exact_fields(
            quantities,
            {"content_pages", "blank_pages", "sheets", "estimated_weight_g"},
            "traveller.quantities",
        )
        for name in ("content_pages", "blank_pages", "sheets"):
            if _integer(quantities[name], f"traveller.quantities.{name}") < 0:
                raise ValueError(f"traveller.quantities.{name} must not be negative")
        _nonnegative_decimal(
            quantities["estimated_weight_g"],
            "traveller.quantities.estimated_weight_g",
        )
        franking = _object(data["franking"], "traveller.franking")
        _validate_franking(franking)
        return cls(
            _string(data["template_revision"], "traveller.template_revision"),
            service,
            recipient,
            quantities,
            franking,
        )


@dataclass(frozen=True, slots=True)
class PrintJob:
    id: str
    sequence: int
    kind: PrintJobKind

    @classmethod
    def from_dict(cls, value: object) -> PrintJob:
        data = _object(value, "job")
        _exact_fields(data, {"id", "sequence", "kind"}, "job")
        result = cls(
            _uuid(data["id"], "job.id"),
            _integer(data["sequence"], "job.sequence"),
            _enum(PrintJobKind, data["kind"], "job.kind"),
        )
        if result.sequence < 1:
            raise ValueError("job.sequence must be positive")
        return result


@dataclass(frozen=True, slots=True)
class PackageLetter:
    operation_id: str
    task_id: str
    plan_sha256: str
    sequence: int
    generation: int
    job: PrintJob
    sources: tuple[SourceDocument, ...]
    print_instructions: PrintInstructions
    traveller: TravellerInstructions

    @classmethod
    def from_dict(cls, value: object) -> PackageLetter:
        data = _object(value, "letter")
        _exact_fields(
            data,
            {
                "operation_id",
                "task_id",
                "plan_sha256",
                "sequence",
                "generation",
                "job",
                "sources",
                "print",
                "traveller",
            },
            "letter",
        )
        raw_sources = data["sources"]
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("letter.sources must be a non-empty array")
        sources = tuple(SourceDocument.from_dict(item) for item in raw_sources)
        result = cls(
            _uuid(data["operation_id"], "letter.operation_id"),
            _uuid(data["task_id"], "letter.task_id"),
            _string(data["plan_sha256"], "letter.plan_sha256"),
            _integer(data["sequence"], "letter.sequence"),
            _integer(data["generation"], "letter.generation"),
            PrintJob.from_dict(data["job"]),
            sources,
            PrintInstructions.from_dict(data["print"]),
            TravellerInstructions.from_dict(data["traveller"]),
        )
        _require_sha256(result.plan_sha256, "letter.plan_sha256")
        if result.sequence < 1 or result.generation < 1:
            raise ValueError("letter sequence and generation must be positive")
        if tuple(source.ordinal for source in sources) != tuple(
            range(1, len(sources) + 1)
        ):
            raise ValueError(
                "letter sources must have contiguous ordinals starting at one"
            )
        if len({source.path for source in sources}) != len(sources):
            raise ValueError("letter source paths must be unique")
        return result


@dataclass(frozen=True, slots=True)
class PackageManifest:
    run_id: str
    letters: tuple[PackageLetter, ...]

    @property
    def jobs(self) -> tuple[PrintJob, ...]:
        return tuple(letter.job for letter in self.letters)

    @classmethod
    def from_dict(cls, value: object) -> PackageManifest:
        data = _object(value, "manifest")
        _exact_fields(data, {"schema", "run_id", "letters"}, "manifest")
        if data["schema"] != "xarta.postal.production-package/v3":
            raise ValueError("unsupported package manifest schema")
        raw_letters = data["letters"]
        if not isinstance(raw_letters, list) or not raw_letters:
            raise ValueError("manifest.letters must be a non-empty array")
        letters = tuple(PackageLetter.from_dict(item) for item in raw_letters)
        expected = tuple(range(1, len(letters) + 1))
        if (
            tuple(letter.sequence for letter in letters) != expected
            or tuple(letter.job.sequence for letter in letters) != expected
        ):
            raise ValueError(
                "manifest letters and jobs must have contiguous sequence order"
            )
        if len({letter.job.id for letter in letters}) != len(letters) or len(
            {letter.task_id for letter in letters}
        ) != len(letters):
            raise ValueError("manifest job and task ids must be unique")
        return cls(_uuid(data["run_id"], "manifest.run_id"), letters)


@dataclass(frozen=True, slots=True)
class VerifiedPackage:
    manifest: PackageManifest
    root: Path

    def path_for(self, source: SourceDocument) -> Path:
        return self.root.joinpath(*source.path.split("/"))


@dataclass(frozen=True, slots=True)
class ActiveRun:
    id: str
    package_acknowledged: bool

    @classmethod
    def from_dict(cls, value: object) -> ActiveRun:
        data = _object(value, "active run")
        _exact_fields(data, {"id", "package_acknowledged"}, "active run")
        if not isinstance(data["package_acknowledged"], bool):
            raise ValueError("active run.package_acknowledged must be a boolean")
        return cls(_uuid(data["id"], "active run.id"), data["package_acknowledged"])


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    id: str
    claim_token: str
    task_ids: tuple[str, ...]
    duplicate: bool

    @classmethod
    def from_dict(cls, value: object) -> ClaimedRun:
        data = _object(value, "claimed run")
        _exact_fields(
            data,
            {"id", "station_id", "site_id", "claim_token", "task_ids", "duplicate"},
            "claimed run",
        )
        if not isinstance(data["task_ids"], list) or not isinstance(
            data["duplicate"], bool
        ):
            raise ValueError("invalid claimed run")
        return cls(
            _uuid(data["id"], "claimed run.id"),
            _string(data["claim_token"], "claimed run.claim_token"),
            tuple(_uuid(item, "claimed run.task_id") for item in data["task_ids"]),
            data["duplicate"],
        )


@dataclass(frozen=True, slots=True)
class HandoverBatch:
    id: str
    duplicate: bool

    @classmethod
    def from_dict(cls, value: object) -> HandoverBatch:
        data = _object(value, "handover batch")
        _exact_fields(data, {"id", "duplicate"}, "handover batch")
        if not isinstance(data["duplicate"], bool):
            raise ValueError("handover batch.duplicate must be a boolean")
        return cls(_uuid(data["id"], "handover batch.id"), data["duplicate"])


@dataclass(frozen=True, slots=True)
class PrintAttempt:
    id: str
    job_id: str
    generation: int
    attempt: int
    job_name: str
    rendered_sha256: str
    rendered_byte_count: int

    @classmethod
    def from_dict(cls, value: object) -> PrintAttempt:
        data = _object(value, "print attempt")
        _exact_fields(
            data,
            {
                "id",
                "job_id",
                "generation",
                "attempt",
                "job_name",
                "rendered_sha256",
                "rendered_byte_count",
            },
            "print attempt",
        )
        result = cls(
            _uuid(data["id"], "print attempt.id"),
            _uuid(data["job_id"], "print attempt.job_id"),
            _integer(data["generation"], "print attempt.generation"),
            _integer(data["attempt"], "print attempt.attempt"),
            _string(data["job_name"], "print attempt.job_name"),
            _string(data["rendered_sha256"], "print attempt.rendered_sha256"),
            _integer(data["rendered_byte_count"], "print attempt.rendered_byte_count"),
        )
        _require_sha256(result.rendered_sha256, "print attempt.rendered_sha256")
        if min(result.generation, result.attempt, result.rendered_byte_count) < 1:
            raise ValueError(
                "print attempt generation, attempt, and rendered size must be positive"
            )
        return result


@dataclass(frozen=True, slots=True)
class ScanAcknowledgement:
    task_id: str
    mode: ScanMode
    stage: ProductionStage
    duplicate: bool

    @classmethod
    def from_dict(cls, value: object) -> ScanAcknowledgement:
        data = _object(value, "scan acknowledgement")
        _exact_fields(
            data, {"task_id", "mode", "stage", "duplicate"}, "scan acknowledgement"
        )
        if not isinstance(data["duplicate"], bool):
            raise ValueError("scan acknowledgement.duplicate must be a boolean")
        return cls(
            _uuid(data["task_id"], "scan acknowledgement.task_id"),
            _enum(ScanMode, data["mode"], "scan acknowledgement.mode"),
            _enum(ProductionStage, data["stage"], "scan acknowledgement.stage"),
            data["duplicate"],
        )


def _validate_franking(data: Mapping[str, Any]) -> None:
    _exact_fields(
        data,
        {"provider", "method", "policy_revision", "supplies", "pricing"},
        "traveller.franking",
    )
    provider = _object(data["provider"], "traveller.franking.provider")
    _exact_fields(provider, {"id", "profile"}, "traveller.franking.provider")
    _exact_fields(
        _object(provider["profile"], "traveller.franking.provider.profile"),
        {"id", "revision"},
        "traveller.franking.provider.profile",
    )
    _string(provider["id"], "traveller.franking.provider.id")
    profile = _object(provider["profile"], "traveller.franking.provider.profile")
    _string(profile["id"], "traveller.franking.provider.profile.id")
    _string(profile["revision"], "traveller.franking.provider.profile.revision")
    _string(data["method"], "traveller.franking.method")
    _string(data["policy_revision"], "traveller.franking.policy_revision")
    supplies = data["supplies"]
    if not isinstance(supplies, list):
        raise ValueError("traveller.franking.supplies must be an array")
    for supply in supplies:
        value = _object(supply, "traveller.franking.supply")
        _exact_fields(
            value,
            {"reference", "description", "quantity"},
            "traveller.franking.supply",
        )
        _string(value["reference"], "traveller.franking.supply.reference")
        _string(value["description"], "traveller.franking.supply.description")
        if _integer(value["quantity"], "traveller.franking.supply.quantity") < 1:
            raise ValueError("traveller.franking.supply.quantity must be positive")
    pricing = _object(data["pricing"], "traveller.franking.pricing")
    _exact_fields(
        pricing,
        {"tariff_revision", "currency", "lines", "total_postage"},
        "traveller.franking.pricing",
    )
    if not isinstance(pricing["lines"], list):
        raise ValueError("traveller.franking.pricing.lines must be an array")
    for line in pricing["lines"]:
        value = _object(line, "traveller.franking.pricing.line")
        _exact_fields(
            value,
            {"reference", "quantity", "unit_price", "amount"},
            "traveller.franking.pricing.line",
        )
        _string(value["reference"], "traveller.franking.pricing.line.reference")
        if _integer(value["quantity"], "traveller.franking.pricing.line.quantity") < 1:
            raise ValueError(
                "traveller.franking.pricing.line.quantity must be positive"
            )
        _nonnegative_decimal(
            value["unit_price"], "traveller.franking.pricing.line.unit_price"
        )
        _nonnegative_decimal(value["amount"], "traveller.franking.pricing.line.amount")
    _string(pricing["tariff_revision"], "traveller.franking.pricing.tariff_revision")
    _string(pricing["currency"], "traveller.franking.pricing.currency")
    _nonnegative_decimal(
        pricing["total_postage"], "traveller.franking.pricing.total_postage"
    )


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _exact_fields(data: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(data) != expected:
        raise ValueError(
            f"invalid {name} fields; missing={sorted(expected - set(data))}, unknown={sorted(set(data) - expected)}"
        )


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _uuid(value: object, name: str) -> str:
    text = _string(value, name)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise ValueError(f"{name} must be a UUID") from error
    if str(parsed) != text:
        raise ValueError(f"{name} must be a canonical UUID")
    return text


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _enum(enum_type: type[StrEnum], value: object, name: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"invalid {name}: {value}") from error


def _require_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _nonnegative_decimal(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal string") from error
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be non-negative and finite")
