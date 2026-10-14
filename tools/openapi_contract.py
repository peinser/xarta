"""Refresh executable schemas and check the checked-in OpenAPI contract offline."""

from __future__ import annotations

import argparse
import ast
import json
import re

from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for

from xarta.protocol.dag import node_contract
from xarta.protocol.dag import supported_node_kinds
from xarta.protocol.dag.contracts import flow_schema
from xarta.protocol.document.request.schema import SCHEMA_DOCUMENT_RENDER_REQUEST

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/openapi/v1/openapi.json"
METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def executable_schemas() -> dict:
    contracts = [node_contract(kind) for kind in sorted(supported_node_kinds())]
    definitions = flow_schema(contracts)["$defs"]
    # Move the existing local definition pointers into the OpenAPI components.
    definitions = json.loads(
        json.dumps(definitions).replace("#/$defs/", "#/components/schemas/Workflow_")
    )
    schemas = {f"Workflow_{name}": value for name, value in definitions.items()}
    for contract in contracts:
        schema = schemas[f"Workflow_node_{contract['kind']}"]
        schema["description"] = contract["description"]
        schema["examples"] = [contract["example"]]
        schema["x-outcomes"] = contract["outcomes"]
        if contract.get("runtime_constraints"):
            schema["x-runtime-constraints"] = contract["runtime_constraints"]
    schemas["RenderRequest"] = SCHEMA_DOCUMENT_RENDER_REQUEST
    preview = deepcopy(SCHEMA_DOCUMENT_RENDER_REQUEST)
    preview["description"] = (
        "Render request whose payload.content_type may be omitted (defaults to application/json for JSON input)."
    )
    preview["properties"]["payload"]["required"] = ["data"]
    schemas["PreviewRequest"] = preview
    document_check = deepcopy(SCHEMA_DOCUMENT_RENDER_REQUEST)
    document_check["required"] = ["payload"]
    schemas["DocumentTypeCheckRequest"] = document_check
    return schemas


def implemented_routes() -> set[tuple[str, str]]:
    # Ponytail: inventory literal Sanic decorators/add_route calls only. If route
    # registration becomes dynamic, replace this with an application-router export.
    routes = set()
    sources = list((ROOT / "src/xarta/services/v1").rglob("*.py"))
    sources.append(ROOT / "src/xarta/bin/services.py")
    for source in sources:
        base = source.parent / "base.py"
        prefix = ""
        if base.exists():
            match = re.search(r'url_prefix=["\']([^"\']+)', base.read_text())
            if match:
                prefix = match[1]
        tree = ast.parse(source.read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(
                call.func, ast.Attribute
            ):
                continue
            if not isinstance(call.func.value, ast.Name) or call.func.value.id not in {
                "bp",
                "app",
            }:
                continue
            method = call.func.attr
            if method not in METHODS | {"route", "add_route"}:
                continue
            index = 1 if method == "add_route" else 0
            path = ast.literal_eval(call.args[index])
            methods = [method] if method in METHODS else ["get"]
            for keyword in call.keywords:
                if keyword.arg == "methods":
                    methods = [
                        value.lower() for value in ast.literal_eval(keyword.value)
                    ]
            path = re.sub(r"<([^:>]+)(?::[^>]+)?>", r"{\1}", prefix + path)
            routes.update((verb, path) for verb in methods)
    return routes


def check(document: dict) -> None:
    if document["openapi"] != "3.1.0":
        raise ValueError("Expected OpenAPI 3.1.0")
    expected_schemas = executable_schemas()
    generated_names = {
        name
        for name in document["components"]["schemas"]
        if name.startswith("Workflow_")
    }
    if generated_names != {
        name for name in expected_schemas if name.startswith("Workflow_")
    }:
        raise ValueError("Stale workflow schema inventory; run with --update-schemas")
    for name, expected in expected_schemas.items():
        if document["components"]["schemas"].get(name) != expected:
            raise ValueError(f"Stale schema {name}; run with --update-schemas")
    for schema in document["components"]["schemas"].values():
        validator_for(schema, default=Draft202012Validator).check_schema(schema)

    def references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                if not reference.startswith("#/"):
                    raise ValueError(f"Contract must be self-contained: {reference}")
                target = document
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    references(document)
    validator = Draft202012Validator(document)
    for kind in supported_node_kinds():
        validator.evolve(
            schema={"$ref": f"#/components/schemas/Workflow_node_{kind}"}
        ).validate(node_contract(kind)["example"])
    routes = set()
    operation_ids = set()
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            routes.add((method, path))
            identifier = operation["operationId"]
            if identifier in operation_ids:
                raise ValueError(f"Duplicate operationId: {identifier}")
            operation_ids.add(identifier)
            if not operation.get("responses"):
                raise ValueError(f"Missing responses: {identifier}")
            parameters = item.get("parameters", []) + operation.get("parameters", [])
            path_parameters = {
                p["name"]
                for p in parameters
                if p.get("in") == "path" and p.get("required")
            }
            if path_parameters != set(re.findall(r"\{([^}]+)\}", path)):
                raise ValueError(f"Incorrect path parameters: {identifier}")
            if path.startswith("/api/v1/"):
                if item.get("x-path-alias") != path.replace("/api/v1/", "/api/", 1):
                    raise ValueError(f"Missing floating alias: {path}")
    implemented = implemented_routes()
    if routes != implemented:
        raise ValueError(
            f"Route mismatch: undocumented={implemented - routes}; unimplemented={routes - implemented}"
        )
    print(
        f"OpenAPI contract valid: {len(routes)} operations; {len(supported_node_kinds())} workflow capabilities"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-schemas", action="store_true")
    arguments = parser.parse_args()
    document = json.loads(CONTRACT.read_text())
    if arguments.update_schemas:
        schemas = document["components"]["schemas"]
        for name in list(schemas):
            if name.startswith("Workflow_") or name in {
                "RenderRequest",
                "PreviewRequest",
                "DocumentTypeCheckRequest",
            }:
                del schemas[name]
        schemas.update(executable_schemas())
        CONTRACT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    check(document)


if __name__ == "__main__":
    main()
