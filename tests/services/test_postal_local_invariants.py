from __future__ import annotations

import hashlib
import io
import json
import zipfile

from decimal import Decimal

import pytest

from xarta.services.v1.postal.adapters.local.carrier import CarrierSemanticState
from xarta.services.v1.postal.adapters.local.carrier import carrier_outcome
from xarta.services.v1.postal.adapters.local.carrier import interpret_semantic_state
from xarta.services.v1.postal.adapters.local.models import DocumentBoundaryPolicy
from xarta.services.v1.postal.adapters.local.models import DocumentSnapshot
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
from xarta.services.v1.postal.adapters.local.pricing import ActualProduction
from xarta.services.v1.postal.adapters.local.workflow import PhysicalState
from xarta.services.v1.postal.adapters.local.workflow import PrintJobKind
from xarta.services.v1.postal.adapters.local.workflow import ScanMode
from xarta.services.v1.postal.adapters.local.workflow import reduce_scan


def test_carrier_semantics_map_to_generic_outcomes() -> None:
    delivered = interpret_semantic_state(
        "provider-delivered", CarrierSemanticState.DELIVERED
    )
    assert delivered.outcome == "delivered"
    assert carrier_outcome(CarrierSemanticState.ANNOUNCED) is None

    unknown = interpret_semantic_state("provider-unknown", None)
    assert unknown.semantic_state is None
    assert unknown.requires_reconciliation


def test_plan_digest_canonicalizes_equivalent_decimal_encodings() -> None:
    document = DocumentSnapshot("document", 1, "content", "a" * 64, 1)
    settings = PrintSettings(
        PrintColorMode.MONOCHROME,
        PrintSides.SIMPLEX,
        DocumentBoundaryPolicy.CONTINUOUS,
    )
    first_paper = PaperProfile(
        "a4", "1", Decimal("210.0"), Decimal(297), Decimal(80), Decimal("0.10")
    )
    second_paper = PaperProfile(
        "a4",
        "1",
        Decimal("210.00"),
        Decimal("297.0"),
        Decimal("80.0"),
        Decimal("0.100"),
    )
    first_quantities = calculate_production_quantities(
        (document,), settings, first_paper
    )
    second_quantities = calculate_production_quantities(
        (document,), settings, second_paper
    )
    first = ProductionPlan(
        (document,),
        settings,
        "a4",
        "1",
        first_quantities,
        "c5",
        "1",
        "fold",
        "small",
        first_quantities.content_weight_g + Decimal("4.0"),
        FrankingMethod.STAMPS,
        None,
        "1",
        "1",
    )
    second = ProductionPlan(
        (document,),
        settings,
        "a4",
        "1",
        second_quantities,
        "c5",
        "1",
        "fold",
        "small",
        second_quantities.content_weight_g + Decimal("4.00"),
        FrankingMethod.STAMPS,
        None,
        "1",
        "1",
    )
    assert production_plan_digest(first) == production_plan_digest(second)


def test_package_manifests_pin_artifact_and_source_bytes() -> None:
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
    package = build_production_package("run-1", (letter,))
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema"] == "xarta.postal.production-package/v3"
        assert (
            manifest["letters"][0]["sources"][0]["path"]
            == "letters/000001/sources/001.pdf"
        )
        checksums = archive.read("checksums.sha256").decode().splitlines()
        assert len(checksums) == len(archive.namelist()) - 1


def test_package_rejects_unsafe_or_out_of_order_inputs() -> None:
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
                "../source.pdf",
                source,
                hashlib.sha256(source).hexdigest(),
                len(source),
                1,
                "document",
            ),
        ),
        {},
        {},
    )
    with pytest.raises(ValueError, match="unsafe"):
        build_production_package("run-1", (letter,))


def test_scans_reject_stale_generations() -> None:
    with pytest.raises(ValueError, match="generation"):
        reduce_scan(
            PhysicalState.READY_FOR_PROCESSING,
            ScanMode.START_PRODUCTION,
            generation=1,
            current_generation=2,
        )


def test_actual_production_rejects_non_decimal_or_negative_physical_values() -> None:
    with pytest.raises(TypeError, match="binary float"):
        ActualProduction(1, 1, 1.5, Decimal("0.20"), Decimal("1.40"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="weight_g"):
        ActualProduction(1, 1, Decimal(-1), Decimal("0.20"), Decimal("1.40"))
