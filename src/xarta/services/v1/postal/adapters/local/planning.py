from __future__ import annotations

import hashlib
import json

from dataclasses import fields
from decimal import Decimal
from enum import Enum
from typing import Any

from xarta.services.v1.postal.adapters.local.models import DocumentBoundaryPolicy
from xarta.services.v1.postal.adapters.local.models import DocumentSnapshot
from xarta.services.v1.postal.adapters.local.models import EnvelopeProfile
from xarta.services.v1.postal.adapters.local.models import PaperProfile
from xarta.services.v1.postal.adapters.local.models import PrintColorMode
from xarta.services.v1.postal.adapters.local.models import PrintSettings
from xarta.services.v1.postal.adapters.local.models import PrintSides
from xarta.services.v1.postal.adapters.local.models import ProductionPlan
from xarta.services.v1.postal.adapters.local.models import ProductionQuantities


def calculate_production_quantities(
    documents: tuple[DocumentSnapshot, ...],
    settings: PrintSettings,
    paper: PaperProfile,
) -> ProductionQuantities:
    if not documents:
        raise ValueError("A production plan requires at least one document")
    if tuple(document.ordinal for document in documents) != tuple(
        range(1, len(documents) + 1)
    ):
        raise ValueError("Documents must have contiguous ordinals starting at one")

    content_pages = sum(document.page_count for document in documents)
    blank_pages = 0
    duplex = settings.sides is not PrintSides.SIMPLEX
    if duplex and settings.document_boundary is not DocumentBoundaryPolicy.CONTINUOUS:
        blank_pages = sum(document.page_count % 2 for document in documents[:-1])

    occupied_sides = content_pages + blank_pages
    sheets = occupied_sides if not duplex else (occupied_sides + 1) // 2
    color_impressions = (
        content_pages if settings.color_mode is PrintColorMode.COLOR else 0
    )
    monochrome_impressions = content_pages - color_impressions
    return ProductionQuantities(
        content_pages=content_pages,
        blank_pages=blank_pages,
        sheets=sheets,
        monochrome_impressions=monochrome_impressions,
        color_impressions=color_impressions,
        traveller_sheets=1,
        content_weight_g=paper.sheet_weight_g * sheets,
        content_thickness_mm=paper.thickness_mm * sheets,
    )


def select_envelope(
    candidates: tuple[EnvelopeProfile, ...],
    quantities: ProductionQuantities,
    *,
    fold_profile: str,
    postal_format: str | None = None,
) -> EnvelopeProfile:
    valid = (
        candidate
        for candidate in candidates
        if candidate.fold_profile == fold_profile
        and (postal_format is None or candidate.postal_format == postal_format)
        and quantities.sheets <= candidate.maximum_sheets
        and quantities.content_thickness_mm <= candidate.maximum_content_thickness_mm
        and quantities.content_weight_g + candidate.envelope_weight_g
        <= candidate.maximum_total_weight_g
    )
    try:
        return min(
            valid,
            key=lambda candidate: (
                candidate.total_cost,
                candidate.id,
                candidate.revision,
            ),
        )
    except ValueError as ex:
        raise ValueError("No envelope profile satisfies the production plan") from ex


def production_plan_digest(plan: ProductionPlan) -> str:
    canonical = json.dumps(
        _canonical_value(plan), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(b"xarta-postal-production-plan-v1\0" + canonical).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value.is_zero():
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value
