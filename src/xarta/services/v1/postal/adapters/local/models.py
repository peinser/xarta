from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from enum import StrEnum


def decimal_quantity(value: Decimal, *, name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{name} must not use binary float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError) as ex:
        raise ValueError(f"{name} must be a Decimal-compatible value") from ex
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


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


class FrankingMethod(StrEnum):
    PORT_PAID = "port_paid"
    STAMPS = "stamps"
    FRANKING_MACHINE = "franking_machine"


@dataclass(frozen=True, slots=True)
class PrintSettings:
    color_mode: PrintColorMode
    sides: PrintSides
    document_boundary: DocumentBoundaryPolicy


@dataclass(frozen=True, slots=True)
class PaperProfile:
    id: str
    revision: str
    width_mm: Decimal
    height_mm: Decimal
    grammage_g_m2: Decimal
    thickness_mm: Decimal

    def __post_init__(self) -> None:
        for field_name in ("width_mm", "height_mm", "grammage_g_m2", "thickness_mm"):
            value = decimal_quantity(getattr(self, field_name), name=field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
            object.__setattr__(self, field_name, value)

    @property
    def sheet_weight_g(self) -> Decimal:
        return self.width_mm * self.height_mm * self.grammage_g_m2 / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    id: str
    ordinal: int
    role: str
    sha256: str
    page_count: int

    def __post_init__(self) -> None:
        if not self.id or not self.role:
            raise ValueError("Document ID and role must be non-empty")
        if self.ordinal < 1 or self.page_count < 1:
            raise ValueError("Document ordinal and page count must be positive")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("Document SHA-256 must be lowercase hexadecimal")


@dataclass(frozen=True, slots=True)
class ProductionQuantities:
    content_pages: int
    blank_pages: int
    sheets: int
    monochrome_impressions: int
    color_impressions: int
    traveller_sheets: int
    content_weight_g: Decimal
    content_thickness_mm: Decimal

    def __post_init__(self) -> None:
        for name in (
            "content_pages",
            "blank_pages",
            "sheets",
            "monochrome_impressions",
            "color_impressions",
            "traveller_sheets",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("content_weight_g", "content_thickness_mm"):
            value = decimal_quantity(getattr(self, name), name=name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class EnvelopeProfile:
    id: str
    revision: str
    fold_profile: str
    postal_format: str
    maximum_sheets: int
    maximum_content_thickness_mm: Decimal
    maximum_total_weight_g: Decimal
    envelope_weight_g: Decimal
    envelope_cost: Decimal
    carrier_cost: Decimal
    handling_cost: Decimal

    def __post_init__(self) -> None:
        if (
            not self.id
            or not self.revision
            or not self.fold_profile
            or not self.postal_format
        ):
            raise ValueError(
                "Envelope identity, fold profile, and postal format must be non-empty"
            )
        if self.maximum_sheets < 1:
            raise ValueError("maximum_sheets must be positive")
        for name in (
            "maximum_content_thickness_mm",
            "maximum_total_weight_g",
            "envelope_weight_g",
            "envelope_cost",
            "carrier_cost",
            "handling_cost",
        ):
            value = decimal_quantity(getattr(self, name), name=name)
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)

    @property
    def total_cost(self) -> Decimal:
        return self.envelope_cost + self.carrier_cost + self.handling_cost


@dataclass(frozen=True, slots=True)
class ProductionPlan:
    documents: tuple[DocumentSnapshot, ...]
    print_settings: PrintSettings
    paper_profile_id: str
    paper_profile_revision: str
    quantities: ProductionQuantities
    envelope_profile_id: str
    envelope_profile_revision: str
    fold_profile: str
    postal_format: str
    estimated_total_weight_g: Decimal
    franking_method: FrankingMethod
    franking_profile: str | None
    pricing_revision: str
    traveller_template_revision: str

    def __post_init__(self) -> None:
        if not self.documents:
            raise ValueError("A production plan requires at least one document")
        if tuple(document.ordinal for document in self.documents) != tuple(
            range(1, len(self.documents) + 1)
        ):
            raise ValueError(
                "Production plan documents must have contiguous ordinals starting at one"
            )
        value = decimal_quantity(
            self.estimated_total_weight_g, name="estimated_total_weight_g"
        )
        if value < self.quantities.content_weight_g:
            raise ValueError("Estimated total weight must include all content weight")
        if (
            self.franking_method is FrankingMethod.PORT_PAID
            and not self.franking_profile
        ):
            raise ValueError(
                "Port Paid production plans require a pinned franking profile"
            )
        if (
            self.franking_method is not FrankingMethod.PORT_PAID
            and self.franking_profile is not None
        ):
            raise ValueError(
                "Only Port Paid production plans may pin a franking profile"
            )
        object.__setattr__(self, "estimated_total_weight_g", value)
