from __future__ import annotations

import json

from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker
from sanic.request.parameters import RequestParameters

from xarta.exceptions.http import BadRequestError
from xarta.protocol.document.request.bundle import DocumentBundleRequest
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.intake.base import intake
from xarta.services.v1.intake.base import prepare_flow
from xarta.services.v1.intake.capabilities import CapabilityManifest
from xarta.services.v1.postal.adapters.local.repository import LocalPostalRepository

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/openapi/v1/openapi.json"
)
CONTRACT = json.loads(CONTRACT_PATH.read_text())
VALIDATOR = Draft202012Validator(CONTRACT, format_checker=FormatChecker())


def test_yaml_preserves_the_entire_json_contract() -> None:
    yaml_text = CONTRACT_PATH.with_suffix(".yaml").read_text()
    exported = yaml.safe_load(yaml_text)
    assert exported == CONTRACT
    # YAML 1.2 editors must retain leading zeros in participant identifiers.
    assert "scheme: '0208'" in yaml_text
    assert "identifier: '0308357159'" in yaml_text
    assert (
        CONTRACT.get(
            "jsonSchemaDialect", "https://spec.openapis.org/oas/3.1/dialect/base"
        )
        == "https://spec.openapis.org/oas/3.1/dialect/base"
    )


def test_bundle_filename_schema_matches_parser_boundaries() -> None:
    validator = VALIDATOR.evolve(
        schema=CONTRACT["components"]["schemas"]["BundleRequest"]
    )
    original = CONTRACT["components"]["examples"]["BundleInvoice"]["value"]
    for filename in (
        "invoice.pdf",
        "facture été.pdf",
        None,
        "",
        ".",
        "..",
        "invoices/invoice.pdf",
        "invoices\\invoice.pdf",
        "invoice.pdf\n",
        "bad\0.pdf",
    ):
        for scope in ("document", "download"):
            payload = deepcopy(original)
            if scope == "document":
                payload["documents"][0]["filename"] = filename
            else:
                payload["options"]["filename"] = filename
            expected = filename in ("invoice.pdf", "facture été.pdf", None) or (
                scope == "download" and filename == ""
            )
            assert validator.is_valid(payload) is expected, (scope, filename)
            if expected:
                DocumentBundleRequest.fromdict(payload)
            else:
                with pytest.raises(ValueError, match="plain file names"):
                    DocumentBundleRequest.fromdict(payload)

    defaults = CONTRACT["components"]["examples"]["BundleDefaultFilenames"]["value"]
    validator.validate(defaults)
    DocumentBundleRequest.fromdict(defaults)


def test_doccle_profile_schema_matches_nonblank_validation() -> None:
    validator = VALIDATOR.evolve(
        schema=CONTRACT["components"]["schemas"]["ReceiverProfile"]
    )
    for field in ("label", "first_name", "last_name", "email", "language"):
        # Explicit Unicode whitespace keeps browser and Python validators aligned;
        # JavaScript's \s differs from Python's for U+0085 and U+FEFF.
        for value in ("Customer", "", " \t\n", "\u00a0", "\u0085", "\ufeff", None):
            profile = {"label": "Customer", field: value}
            expected = value in ("Customer", "\ufeff") or (
                field != "label" and value is None
            )
            assert validator.is_valid(profile) is expected, (field, value)
            if expected:
                DoccleReceiverProfile(**profile)
            else:
                with pytest.raises(ValueError, match="non-empty"):
                    DoccleReceiverProfile(**profile)


async def test_handover_task_schema_matches_repository_validation() -> None:
    operation = CONTRACT["paths"]["/api/v1/postal/local/handover-batches"]["post"]
    validator = VALIDATOR.evolve(
        schema=operation["requestBody"]["content"]["application/json"]["schema"]
    )
    payload = deepcopy(
        CONTRACT["components"]["examples"]["PostalHandoverWithoutOperator"]["value"]
    )
    validator.validate(payload)
    task_id = payload["task_ids"][0]
    # Invalid lists must fail before any database access.
    repository = LocalPostalRepository(None)
    for tasks, message in (
        ([], "at least one task"),
        ([task_id, task_id], "must be unique"),
    ):
        payload["task_ids"] = tasks
        assert not validator.is_valid(payload)
        with pytest.raises(ValueError, match=message):
            await repository.create_handover_batch(
                station_id="brussels-mailroom-01",
                site_id="brussels",
                service_date=date.fromisoformat(payload["service_date"]),
                task_ids=tuple(UUID(item) for item in tasks),
                operator_reference=None,
                idempotency_key="contract-review-handover",
            )


async def test_intake_rejects_unknown_fields_across_transports() -> None:
    validator = VALIDATOR.evolve(
        schema=CONTRACT["components"]["schemas"]["FlowSubmission"]
    )
    for definition in (
        {"dag": {"kind": "debug"}, "unexpected": True},
        {"delivery_profile": "development-standard@2", "unexpected": True},
    ):
        assert not validator.is_valid(definition)
        for handler, multipart in (
            (intake, False),
            (intake, True),
            (prepare_flow, False),
        ):
            request = SimpleNamespace(
                files=RequestParameters(
                    {"flow": [SimpleNamespace(body=json.dumps(definition).encode())]}
                    if multipart
                    else {}
                ),
                json=definition,
                app=SimpleNamespace(
                    ctx=SimpleNamespace(
                        intake_capabilities=CapabilityManifest(frozenset({"debug"}))
                    )
                ),
            )
            with pytest.raises(
                BadRequestError, match="Unsupported flow submission fields: unexpected"
            ):
                await handler(request)
