from __future__ import annotations

import asyncio
import contextlib
import hashlib
import sys

from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from xarta.ubl.editor import ROOTS
from xarta.ubl.editor import UBLEditor
from xarta.ubl.editor import UBLError
from xarta.ubl.editor import parse_ubl_document

RULESET_ID = "peppol-bis-billing-3"
RULESET_RELEASE = "2026.05"
RULE_DIRECTORY = Path(__file__).parent / "resources" / "billing-3" / RULESET_RELEASE
RULE_FILES = ("CEN-EN16931-UBL.xslt", "PEPPOL-EN16931-UBL.xslt")
MAX_ISSUES = 1000


@dataclass(frozen=True)
class UBLValidationIssue:
    stage: str
    rule: str
    severity: str
    message: str
    location: str | None = None


@dataclass(frozen=True)
class UBLValidationResult:
    valid: bool
    document_kind: str | None
    checks: dict[str, str]
    issues: tuple[UBLValidationIssue, ...]
    ruleset: dict[str, str] | None = None
    truncated: bool = False

    def dict(self) -> dict[str, Any]:
        return asdict(self)


class UBLValidationUnavailable(RuntimeError):
    """The engine could not complete validation; this is not an invalid invoice."""


class UBLValidator:
    def __init__(
        self,
        editor: UBLEditor,
        *,
        timeout: float = 30.0,
        concurrency: int = 2,
    ) -> None:
        if timeout <= 0 or concurrency < 1:
            raise ValueError("Validation timeout and concurrency must be positive")
        self.editor = editor
        self.timeout = timeout
        self._slots = asyncio.Semaphore(concurrency)
        digest = hashlib.sha256()
        for name in RULE_FILES:
            digest.update((RULE_DIRECTORY / name).read_bytes())
        self.ruleset = {
            "id": RULESET_ID,
            "release": RULESET_RELEASE,
            "sha256": digest.hexdigest(),
        }

    def validate_schema(self, document: bytes) -> UBLValidationResult:
        try:
            root = parse_ubl_document(document, self.editor.max_bytes)
        except UBLError as ex:
            return UBLValidationResult(
                False,
                None,
                {"xml": "failed", "xsd": "not_run"},
                (UBLValidationIssue("xml", ex.code, "fatal", str(ex)),),
            )
        kind = ROOTS.get(root.tag)
        try:
            self.editor.validate(root)
        except UBLError as ex:
            errors = ex.schema_errors
            issues = tuple(
                UBLValidationIssue(
                    "xsd",
                    error.type_name,
                    "fatal",
                    error.message,
                    error.path,
                )
                for error in errors[:MAX_ISSUES]
            ) or (UBLValidationIssue("xsd", ex.code, "fatal", str(ex)),)
            return UBLValidationResult(
                False,
                kind,
                {"xml": "passed", "xsd": "failed"},
                issues,
                truncated=len(errors) > MAX_ISSUES,
            )
        return UBLValidationResult(True, kind, {"xml": "passed", "xsd": "passed"}, ())

    async def validate_business_rules(
        self, document: bytes, profile: str = RULESET_ID
    ) -> UBLValidationResult:
        if profile != RULESET_ID:
            raise ValueError("Unsupported UBL validation profile")
        # The timeout includes waiting for a bounded process slot. Cancellation
        # kills and reaps the child; it cannot leave background native XSLT work.
        try:
            async with asyncio.timeout(self.timeout), self._slots:
                schema = await asyncio.to_thread(self.validate_schema, document)
                if not schema.valid:
                    return UBLValidationResult(
                        False,
                        schema.document_kind,
                        {**schema.checks, "business_rules": "not_run"},
                        schema.issues,
                        self.ruleset,
                        schema.truncated,
                    )
                # The runner re-parses with the hardened parser before Saxon sees it.
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "xarta.ubl.rule_runner",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    stdout, _ = await process.communicate(document)
                    if process.returncode != 0:
                        raise UBLValidationUnavailable(
                            "Business-rule validation engine failed"
                        )
                    payload = orjson.loads(stdout)
                    issues = tuple(
                        UBLValidationIssue(**item) for item in payload["issues"]
                    )
                    return UBLValidationResult(
                        payload["valid"],
                        schema.document_kind,
                        {
                            **schema.checks,
                            "business_rules": (
                                "passed" if payload["valid"] else "failed"
                            ),
                        },
                        issues,
                        self.ruleset,
                        payload["truncated"],
                    )
                finally:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
        except TimeoutError as ex:
            raise UBLValidationUnavailable(
                "Business-rule validation deadline exceeded"
            ) from ex
        except (OSError, orjson.JSONDecodeError, KeyError, TypeError) as ex:
            raise UBLValidationUnavailable(
                "Business-rule validation engine failed"
            ) from ex

    def profiles(self) -> list[dict]:
        return [
            {
                **self.ruleset,
                "document_kinds": list(ROOTS.values()),
                "stages": ["UBL 2.1 XSD", "EN 16931 UBL", "Peppol BIS Billing 3"],
                "belgium": "Includes official scheme 0208 enterprise-number rule PEPPOL-COMMON-R043; country-specific rules apply according to their contexts",
            }
        ]
