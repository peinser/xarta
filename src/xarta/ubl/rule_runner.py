"""Isolated SaxonC XSLT 2.0 runner. Only bundled, trusted rules are executed."""

from __future__ import annotations

import sys

from dataclasses import asdict

import orjson

from lxml import etree  # type: ignore[import-untyped]
from saxonche import PySaxonProcessor  # type: ignore[import-not-found]

from xarta.ubl.editor import parse_ubl_document
from xarta.ubl.validation import MAX_ISSUES
from xarta.ubl.validation import RULE_DIRECTORY
from xarta.ubl.validation import RULE_FILES
from xarta.ubl.validation import UBLValidationIssue


def run(document: bytes) -> dict:
    # Validate XML again at the process boundary before passing it to Saxon's parser.
    root = parse_ubl_document(document, len(document))
    xml = etree.tostring(root, encoding="unicode")
    issues: list[dict] = []
    count = 0
    valid = True
    with PySaxonProcessor(license=False) as processor:
        processor.set_configuration_property(
            "http://saxon.sf.net/feature/allow-external-functions", "false"
        )
        processor.set_configuration_property(
            "http://saxon.sf.net/feature/allowedProtocols", ""
        )
        processor.set_configuration_property(
            "http://saxon.sf.net/feature/parserFeature?uri=http%3A%2F%2Fapache.org%2Fxml%2Ffeatures%2Fdisallow-doctype-decl",
            "true",
        )
        node = processor.parse_xml(xml_text=xml)
        compiler = processor.new_xslt30_processor()
        for filename in RULE_FILES:
            executable = compiler.compile_stylesheet(
                stylesheet_file=str(RULE_DIRECTORY / filename)
            )
            report = executable.transform_to_string(xdm_node=node)
            if not report:
                raise RuntimeError("Missing Schematron report")
            report_root = etree.fromstring(report.encode())
            if report_root.tag != "{http://purl.oclc.org/dsdl/svrl}schematron-output":
                raise RuntimeError("Unexpected Schematron report")
            for item in report_root.findall(
                "{http://purl.oclc.org/dsdl/svrl}failed-assert"
            ):
                severity = item.get("flag", "fatal").lower()
                valid = valid and severity in {"warning", "info"}
                count += 1
                if len(issues) < MAX_ISSUES:
                    text = item.find("{http://purl.oclc.org/dsdl/svrl}text")
                    issues.append(
                        asdict(
                            UBLValidationIssue(
                                "en16931" if filename.startswith("CEN") else "peppol",
                                item.get("id", "unknown"),
                                severity,
                                (
                                    " ".join("".join(text.itertext()).split())
                                    if text is not None
                                    else "Rule failed"
                                ),
                                item.get("location"),
                            )
                        )
                    )
    return {"valid": valid, "issues": issues, "truncated": count > MAX_ISSUES}


if __name__ == "__main__":
    sys.stdout.buffer.write(orjson.dumps(run(sys.stdin.buffer.read())))
