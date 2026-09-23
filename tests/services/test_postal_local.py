from __future__ import annotations

import hashlib
import io
import zipfile

from decimal import Decimal

import pytest

from xarta.services.v1.postal.adapters.local.capacity import CapacityDecision
from xarta.services.v1.postal.adapters.local.capacity import CapacityOverflow
from xarta.services.v1.postal.adapters.local.capacity import CapacityPool
from xarta.services.v1.postal.adapters.local.capacity import capacity_decision
from xarta.services.v1.postal.adapters.local.models import DocumentBoundaryPolicy
from xarta.services.v1.postal.adapters.local.models import DocumentSnapshot
from xarta.services.v1.postal.adapters.local.models import EnvelopeProfile
from xarta.services.v1.postal.adapters.local.models import FrankingMethod
from xarta.services.v1.postal.adapters.local.models import PaperProfile
from xarta.services.v1.postal.adapters.local.models import PrintColorMode
from xarta.services.v1.postal.adapters.local.models import PrintSettings
from xarta.services.v1.postal.adapters.local.models import PrintSides
from xarta.services.v1.postal.adapters.local.models import ProductionPlan
from xarta.services.v1.postal.adapters.local.packages import PackageLetter
from xarta.services.v1.postal.adapters.local.packages import PackageSource
from xarta.services.v1.postal.adapters.local.packages import build_production_package
from xarta.services.v1.postal.adapters.local.planning import calculate_production_quantities  # fmt: skip
from xarta.services.v1.postal.adapters.local.planning import production_plan_digest
from xarta.services.v1.postal.adapters.local.planning import select_envelope
from xarta.services.v1.postal.adapters.local.pricing import ActualProduction
from xarta.services.v1.postal.adapters.local.pricing import PriceLine
from xarta.services.v1.postal.adapters.local.pricing import ProductionBounds
from xarta.services.v1.postal.adapters.local.pricing import validate_production_bounds
from xarta.services.v1.postal.adapters.local.workflow import PhysicalState
from xarta.services.v1.postal.adapters.local.workflow import PrintAttemptEvent
from xarta.services.v1.postal.adapters.local.workflow import PrintJob
from xarta.services.v1.postal.adapters.local.workflow import PrintJobKind
from xarta.services.v1.postal.adapters.local.workflow import PrintJobState
from xarta.services.v1.postal.adapters.local.workflow import ScanMode
from xarta.services.v1.postal.adapters.local.workflow import printer_job_name
from xarta.services.v1.postal.adapters.local.workflow import reduce_print_state
from xarta.services.v1.postal.adapters.local.workflow import reduce_scan


def _documents() -> tuple[DocumentSnapshot, ...]:
    return (
        DocumentSnapshot("cover", 1, "cover-letter", "a" * 64, 3),
        DocumentSnapshot("terms", 2, "terms", "b" * 64, 2),
    )


def _paper() -> PaperProfile:
    return PaperProfile(
        "a4-80", "1", Decimal(210), Decimal(297), Decimal(80), Decimal("0.10")
    )


def test_planning_calculates_boundaries_impressions_and_decimal_physical_quantities() -> (
    None
):
    continuous = calculate_production_quantities(
        _documents(),
        PrintSettings(
            PrintColorMode.MONOCHROME,
            PrintSides.DUPLEX_LONG_EDGE,
            DocumentBoundaryPolicy.CONTINUOUS,
        ),
        _paper(),
    )
    aligned = calculate_production_quantities(
        _documents(),
        PrintSettings(
            PrintColorMode.COLOR,
            PrintSides.DUPLEX_SHORT_EDGE,
            DocumentBoundaryPolicy.START_ON_RECTO,
        ),
        _paper(),
    )
    simplex = calculate_production_quantities(
        _documents(),
        PrintSettings(
            PrintColorMode.MONOCHROME,
            PrintSides.SIMPLEX,
            DocumentBoundaryPolicy.START_ON_NEW_SHEET,
        ),
        _paper(),
    )

    assert (continuous.content_pages, continuous.blank_pages, continuous.sheets) == (
        5,
        0,
        3,
    )
    assert (aligned.blank_pages, aligned.sheets, aligned.color_impressions) == (1, 3, 5)
    assert (simplex.blank_pages, simplex.sheets, simplex.monochrome_impressions) == (
        0,
        5,
        5,
    )
    assert aligned.content_weight_g == Decimal("14.9688")
    assert aligned.content_thickness_mm == Decimal("0.30")
    assert aligned.traveller_sheets == 1


def test_planning_rejects_float_quantities_and_selects_lowest_total_valid_envelope() -> (
    None
):
    with pytest.raises(TypeError, match="binary float"):
        PaperProfile("bad", "1", 210.0, Decimal(297), Decimal(80), Decimal("0.1"))  # type: ignore[arg-type]

    quantities = calculate_production_quantities(
        _documents(),
        PrintSettings(
            PrintColorMode.MONOCHROME,
            PrintSides.DUPLEX_LONG_EDGE,
            DocumentBoundaryPolicy.CONTINUOUS,
        ),
        _paper(),
    )
    expensive_small = EnvelopeProfile(
        "small",
        "1",
        "tri-fold",
        "small",
        3,
        Decimal(1),
        Decimal(50),
        Decimal(4),
        Decimal("0.10"),
        Decimal(2),
        Decimal(1),
    )
    cheaper_large = EnvelopeProfile(
        "large",
        "1",
        "tri-fold",
        "large",
        10,
        Decimal(2),
        Decimal(100),
        Decimal(6),
        Decimal("0.40"),
        Decimal(1),
        Decimal("0.20"),
    )

    assert (
        select_envelope(
            (expensive_small, cheaper_large), quantities, fold_profile="tri-fold"
        )
        is cheaper_large
    )


def test_production_plan_digest_is_deterministic_and_semantic() -> None:
    settings = PrintSettings(
        PrintColorMode.MONOCHROME, PrintSides.SIMPLEX, DocumentBoundaryPolicy.CONTINUOUS
    )
    quantities = calculate_production_quantities(_documents(), settings, _paper())
    plan = ProductionPlan(
        _documents(),
        settings,
        "a4-80",
        "1",
        quantities,
        "c5",
        "2",
        "tri-fold",
        "small",
        quantities.content_weight_g + Decimal(4),
        FrankingMethod.STAMPS,
        None,
        "tariff-1",
        "traveller-3",
    )

    assert production_plan_digest(plan) == production_plan_digest(plan)
    changed = ProductionPlan(
        _documents(),
        settings,
        "a4-80",
        "1",
        quantities,
        "c5",
        "2",
        "tri-fold",
        "small",
        plan.estimated_total_weight_g,
        FrankingMethod.STAMPS,
        None,
        "tariff-2",
        "traveller-3",
    )
    assert production_plan_digest(plan) != production_plan_digest(changed)


@pytest.mark.parametrize(
    ("limit", "admitted", "decision"),
    [
        (-1, 1_000_000, CapacityDecision.ADMIT),
        (0, 0, CapacityDecision.QUEUE),
        (2, 1, CapacityDecision.ADMIT),
        (2, 2, CapacityDecision.QUEUE),
    ],
)
def test_capacity_semantics(
    limit: int, admitted: int, decision: CapacityDecision
) -> None:
    pool = CapacityPool("letters", "Europe/Brussels", limit, CapacityOverflow.QUEUE)
    assert capacity_decision(pool, admitted=admitted) is decision


def test_production_package_is_deterministic_manifested_and_checksummed() -> None:
    source = b"source"
    letter = PackageLetter(
        "operation-1",
        "task-1",
        "d" * 64,
        1,
        1,
        "job-1",
        (
            PackageSource(
                1,
                "letters/000001/sources/001.pdf",
                source,
                hashlib.sha256(source).hexdigest(),
                len(source),
                1,
                "document",
            ),
        ),
        {
            "color_mode": "color",
            "sides": "duplex_long_edge",
            "document_boundary": "continuous",
        },
        {},
    )
    first = build_production_package("run-1", (letter,))
    second = build_production_package("run-1", (letter,))

    assert first.content == second.content
    assert first.checksum == hashlib.sha256(first.content).hexdigest()
    assert first.byte_count == len(first.content)
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert archive.read("letters/000001/sources/001.pdf") == source
        assert b"letters/000001/sources/001.pdf" in archive.read("checksums.sha256")
        assert (
            first.manifest_checksum
            == hashlib.sha256(archive.read("manifest.json")).hexdigest()
        )


def test_print_names_and_explicit_scan_reducer() -> None:
    job = PrintJob("job", "run", 2, PrintJobKind.LETTER, 3)
    assert printer_job_name(job, 4) == "postal/run/000002/letter/3/4"
    submitting = reduce_print_state(PrintJobState.PENDING, PrintAttemptEvent.SUBMIT)
    printing = reduce_print_state(submitting, PrintAttemptEvent.ACCEPTED)
    assert (
        reduce_print_state(printing, PrintAttemptEvent.COMPLETED)
        is PrintJobState.COMPLETED
    )
    with pytest.raises(ValueError, match="invalid"):
        reduce_print_state(PrintJobState.UNCERTAIN, PrintAttemptEvent.SUBMIT)
    first = reduce_scan(PhysicalState.READY_FOR_PROCESSING, ScanMode.START_PRODUCTION)
    second = reduce_scan(first.state, ScanMode.READY_FOR_HANDOVER)
    third = reduce_scan(second.state, ScanMode.CONFIRM_HANDOVER)
    assert (first.state, second.outcomes, third.outcomes) == (
        PhysicalState.PROCESSING,
        ("prepared",),
        ("handed_over",),
    )
    assert reduce_scan(
        third.state, ScanMode.CONFIRM_HANDOVER, already_recorded=True
    ).duplicate
    with pytest.raises(ValueError, match="invalid"):
        reduce_scan(PhysicalState.PROCESSING, ScanMode.CONFIRM_HANDOVER)
    with pytest.raises(ValueError, match="handover batch"):
        reduce_scan(
            PhysicalState.READY_FOR_HANDOVER,
            ScanMode.CONFIRM_HANDOVER,
            handover_batch_matches=False,
        )


def test_neutral_exact_price_lines_and_production_bounds() -> None:
    assert PriceLine(
        "paper", Decimal(3), Decimal("0.05"), Decimal("0.15")
    ).amount == Decimal("0.15")
    with pytest.raises(ValueError, match="exact amount"):
        PriceLine("paper", Decimal(3), Decimal("0.05"), Decimal("0.16"))
    bounds = ProductionBounds(5, 3, Decimal(30), Decimal("0.30"), Decimal("1.50"))
    validate_production_bounds(
        bounds, ActualProduction(5, 3, Decimal(20), Decimal("0.20"), Decimal("1.40"))
    )
    with pytest.raises(ValueError, match="pages, weight"):
        validate_production_bounds(
            bounds,
            ActualProduction(6, 3, Decimal(31), Decimal("0.20"), Decimal("1.40")),
        )
