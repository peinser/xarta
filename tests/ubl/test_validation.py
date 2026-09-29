from __future__ import annotations

import asyncio
import hashlib
import json

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

from lxml import etree

from xarta.services.v1.ubl.api import validate_business_rules
from xarta.services.v1.ubl.api import validate_schema
from xarta.ubl.editor import CAC
from xarta.ubl.editor import CBC
from xarta.ubl.editor import UBLAttachment
from xarta.ubl.editor import UBLEditor
from xarta.ubl.validation import RULE_DIRECTORY
from xarta.ubl.validation import UBLValidationUnavailable
from xarta.ubl.validation import UBLValidator

RESOURCES = RULE_DIRECTORY.parents[1]


@pytest.fixture
def validator(editor):
    return UBLValidator(editor)


def example(name="base-example.xml"):
    return (RESOURCES / "billing-3" / "examples" / name).read_bytes()


def test_vendored_resources_integrity():
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((RESOURCES / name).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("name", ["base-example.xml", "base-creditnote-correction.xml"])
async def test_official_valid_billing_examples(validator, name):
    result = await validator.validate_business_rules(example(name))
    assert result.valid, result.issues
    assert result.ruleset["release"] == "2026.05"
    assert result.checks == {
        "xml": "passed",
        "xsd": "passed",
        "business_rules": "passed",
    }


async def test_belgian_number_validation_and_multiple_failures(validator):
    root = etree.fromstring(example())
    for party in root.findall(f"{{{CAC}}}AccountingSupplierParty") + root.findall(
        f"{{{CAC}}}AccountingCustomerParty"
    ):
        endpoint = party.find(f"{{{CAC}}}Party/{{{CBC}}}EndpointID")
        endpoint.set("schemeID", "0208")
        endpoint.text = "0123456789"  # invalid Belgian checksum
        country = party.find(
            f"{{{CAC}}}Party/{{{CAC}}}PostalAddress/{{{CAC}}}Country/{{{CBC}}}IdentificationCode"
        )
        country.text = "BE"
    broken = await validator.validate_business_rules(etree.tostring(root))
    assert not broken.valid
    assert sum(issue.rule == "PEPPOL-COMMON-R043" for issue in broken.issues) == 2
    for endpoint in root.findall(f".//{{{CBC}}}EndpointID"):
        endpoint.text = "0308357159"  # valid checksum
    repaired = await validator.validate_business_rules(etree.tostring(root))
    assert repaired.valid, repaired.issues


async def test_generic_attachment_allowed_but_billing_subset_enforced(validator):
    edited = UBLEditor().edit(
        example(), (UBLAttachment("text", "readme.txt", "text/plain", b"Evidence"),)
    )
    assert validator.validate_schema(edited).valid
    result = await validator.validate_business_rules(edited)
    assert not result.valid
    assert "PEPPOL-EN16931-CL001" in {item.rule for item in result.issues}


async def test_accounting_arithmetic_rules_are_actually_executed(validator):
    root = etree.fromstring(example())
    root.find(f"{{{CAC}}}LegalMonetaryTotal/{{{CBC}}}PayableAmount").text = "999999"
    result = await validator.validate_business_rules(etree.tostring(root))
    assert not result.valid
    assert any(
        item.stage == "en16931" and item.rule.startswith("BR-")
        for item in result.issues
    )


async def test_schema_errors_prevent_schematron_execution(validator, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid XML must not reach the XSLT process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = await validator.validate_business_rules(b"<Invoice/>")
    assert not result.valid and result.checks["business_rules"] == "not_run"


@pytest.mark.parametrize(
    "xml", [b"<bad", b'<!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>']
)
def test_schema_api_reports_xml_failures(validator, xml):
    result = validator.validate_schema(xml)
    assert not result.valid and result.checks["xml"] == "failed"


def test_concurrent_schema_validation_reports_only_own_errors(validator, invoice):
    # The editor's XSD error log is shared state; without its lock, results mix.
    documents = [invoice(references=f"<cbc:Unexpected{index}/>") for index in range(8)]
    expected = [validator.validate_schema(document) for document in documents]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(validator.validate_schema, documents * 25))
    assert results == expected * 25


async def test_engine_timeout_kills_and_reaps_process(validator, monkeypatch):
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, data):
            await asyncio.sleep(10)

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    validator.timeout = 0.2
    with pytest.raises(UBLValidationUnavailable, match="deadline"):
        await validator.validate_business_rules(example())
    assert process.killed and process.waited


def request(validator, body, content_type="application/xml", **args):
    return SimpleNamespace(
        app=SimpleNamespace(ctx=SimpleNamespace(ubl_validator=validator)),
        body=body,
        content_type=content_type,
        args=args,
    )


async def test_http_validation_status_semantics(validator, monkeypatch):
    result = await validate_schema(request(validator, b"<bad"))
    assert result.status == 200 and orjson.loads(result.body)["valid"] is False
    result = await validate_schema(request(validator, b"{}", "application/json"))
    assert result.status == 415
    result = await validate_business_rules(
        request(validator, example(), profile="unknown")
    )
    assert result.status == 400

    async def unavailable(*args, **kwargs):
        raise UBLValidationUnavailable("Engine failed")

    monkeypatch.setattr(validator, "validate_business_rules", unavailable)
    result = await validate_business_rules(request(validator, example()))
    assert result.status == 503 and orjson.loads(result.body)["valid"] is None
