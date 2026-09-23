"""Machine-readable contracts for constructing document workflow DAGs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

UUID = {"type": "string", "format": "uuid"}
NON_EMPTY_STRING = {"type": "string", "minLength": 1}
OPTIONAL_STRING = {"type": ["string", "null"]}


def _object(
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    additional_properties: bool | dict[str, Any] = False,
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


DOCUMENT_SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "generated_document": _object(
        {
            "source": {"const": "generate"},
            "id": UUID,
        },
        ("id",),
        description="A document produced or uploaded under this flow's generated-document ID.",
    ),
    "archived_document": _object(
        {
            "source": {"const": "archive"},
            "id": UUID,
            "archive": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
                "default": "default",
            },
            "version": {"oneOf": [UUID, {"type": "null"}]},
            "representation": {"oneOf": [UUID, {"type": "null"}]},
        },
        ("source", "id"),
        description="An immutable document, optionally pinned to one archive version.",
    ),
    "render_payload": _object(
        {
            "content_type": {
                "enum": [
                    "application/json",
                    "application/json+xml",
                    "application/xml",
                    "text/xml",
                ]
            },
            "data": {"type": ["object", "string"]},
        },
        ("content_type", "data"),
    ),
    "rendered_document": _object(
        {
            "source": {"const": "render"},
            "id": {"oneOf": [UUID, {"type": "null"}]},
            "document_type": NON_EMPTY_STRING,
            "payload": {"$ref": "#/$defs/render_payload"},
            "template_engine": OPTIONAL_STRING,
            "template_engines_options": {"type": ["object", "null"]},
            "parameters": {"type": ["object", "null"]},
            "content_type": OPTIONAL_STRING,
            "metadata": {"type": ["object", "null"]},
        },
        ("source", "document_type", "payload"),
        description="A document rendered from typed payload data during workflow execution.",
    ),
    "bundle_request_document": _object(
        {"id": UUID, "filename": OPTIONAL_STRING},
        ("id",),
    ),
    "bundle_request": _object(
        {
            "id": UUID,
            "documents": {
                "type": "array",
                "minItems": 1,
                "maxItems": 25,
                "items": {"$ref": "#/$defs/bundle_request_document"},
            },
            "options": _object(
                {
                    "filename": OPTIONAL_STRING,
                    "compression": {"$ref": "#/$defs/compression"},
                }
            ),
        },
        ("documents",),
    ),
    "bundled_document": _object(
        {
            "source": {"const": "bundle"},
            "id": UUID,
            "request": {"$ref": "#/$defs/bundle_request"},
        },
        ("source", "id", "request"),
        description="A ZIP document built from other document IDs.",
    ),
    "document_source": {
        "oneOf": [
            {"$ref": "#/$defs/generated_document"},
            {"$ref": "#/$defs/archived_document"},
            {"$ref": "#/$defs/rendered_document"},
            {"$ref": "#/$defs/bundled_document"},
        ]
    },
    "compression": _object(
        {
            "method": {
                "enum": ["stored", "deflated", "bzip2", "lzma"],
                "default": "deflated",
            },
            "level": {"type": "integer", "minimum": 0, "maximum": 9, "default": 9},
        }
    ),
}

DOCUMENT_SOURCE_DEFINITIONS["bundle_node_document"] = {
    "oneOf": [
        _object(
            {
                **deepcopy(DOCUMENT_SOURCE_DEFINITIONS[name]["properties"]),
                "filename": NON_EMPTY_STRING,
            },
            (*DOCUMENT_SOURCE_DEFINITIONS[name].get("required", []), "filename"),
        )
        for name in (
            "generated_document",
            "archived_document",
            "rendered_document",
            "bundled_document",
        )
    ]
}

ARCHIVE_REPRESENTATION = _object(
    {
        "representation_id": {"oneOf": [UUID, {"type": "null"}]},
        "name": {"type": ["string", "null"], "minLength": 1},
        "metadata": {"type": "object"},
        "source": {"$ref": "#/$defs/document_source"},
    },
    ("source",),
)
ARCHIVE_REPRESENTATION_WITH_ID = deepcopy(ARCHIVE_REPRESENTATION)
ARCHIVE_REPRESENTATION_WITH_ID["required"] = ["source", "representation_id"]
DOCUMENT_SOURCE_DEFINITIONS["archive_version"] = {
    **_object(
        {
            "archive": {
                "type": ["string", "null"],
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
            },
            "document_id": {"oneOf": [UUID, {"type": "null"}]},
            "version_id": {"oneOf": [UUID, {"type": "null"}]},
            "parent_version_id": {"oneOf": [UUID, {"type": "null"}]},
            "created": {"type": ["string", "null"], "format": "date-time"},
            "expires": {"type": ["string", "null"], "format": "date-time"},
            "document_type": {"type": ["string", "null"], "minLength": 1},
            "metadata": {"type": "object"},
            "default_representation_id": {"oneOf": [UUID, {"type": "null"}]},
            "representations": {
                "type": "array",
                "minItems": 1,
                "items": ARCHIVE_REPRESENTATION,
            },
        },
        ("representations",),
    ),
    "allOf": [
        {
            "if": {
                "properties": {"representations": {"minItems": 2}},
                "required": ["representations"],
            },
            "then": {
                "required": ["default_representation_id"],
                "properties": {
                    "representations": {
                        "type": "array",
                        "items": ARCHIVE_REPRESENTATION_WITH_ID,
                    }
                },
            },
        }
    ],
}


POSTAL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "postal_generated_document": _object(
        {
            "source": {"const": "generate"},
            "id": UUID,
            "role": {"type": ["string", "null"], "minLength": 1, "maxLength": 64},
        },
        ("source", "id"),
    ),
    "postal_archived_document": _object(
        {
            **deepcopy(DOCUMENT_SOURCE_DEFINITIONS["archived_document"]["properties"]),
            "role": {"type": ["string", "null"], "minLength": 1, "maxLength": 64},
        },
        ("source", "id"),
    ),
    "postal_rendered_document": _object(
        {
            **deepcopy(DOCUMENT_SOURCE_DEFINITIONS["rendered_document"]["properties"]),
            "id": UUID,
            "role": {"type": ["string", "null"], "minLength": 1, "maxLength": 64},
        },
        ("source", "id", "document_type", "payload"),
    ),
    "postal_document": {
        "oneOf": [
            {"$ref": "#/$defs/postal_generated_document"},
            {"$ref": "#/$defs/postal_archived_document"},
            {"$ref": "#/$defs/postal_rendered_document"},
        ]
    },
    "belgian_address": _object(
        {
            "format": {"const": "structured"},
            "street": {"type": "string", "minLength": 1, "maxLength": 128},
            "house_number": {"type": "string", "minLength": 1, "maxLength": 16},
            "box_number": {"type": ["string", "null"], "minLength": 1, "maxLength": 16},
            "postal_code": {"type": "string", "pattern": "^[1-9][0-9]{3}$"},
            "city": {"type": "string", "minLength": 1, "maxLength": 128},
            "region": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
            "country_code": {"const": "BE"},
        },
        ("format", "street", "house_number", "postal_code", "city", "country_code"),
    ),
    "postal_recipient": {
        **_object(
            {
                "name": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
                "company_name": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 128,
                },
                "department": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 128,
                },
                "address": {"$ref": "#/$defs/belgian_address"},
            },
            ("address",),
        ),
        "anyOf": [
            {
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
            },
            {
                "properties": {"company_name": {"type": "string", "minLength": 1}},
                "required": ["company_name"],
            },
        ],
    },
    "postal_sender": _object(
        {
            "type": {"const": "profile"},
            "id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
            },
        },
        ("type", "id"),
    ),
    "postal_service": {
        "oneOf": [
            _object(
                {
                    "type": {"const": "ordinary"},
                    "version": {"const": 1},
                    "speed": {"enum": ["priority", "non_priority"]},
                },
                ("type", "version", "speed"),
            ),
            _object(
                {
                    "type": {"const": "registered"},
                    "version": {"const": 1},
                    "acknowledgement": {"const": "none"},
                },
                ("type", "version", "acknowledgement"),
            ),
        ]
    },
    "postal_print": _object(
        {
            "version": {"const": 1},
            "color_mode": {"enum": ["monochrome", "color"]},
            "sides": {"enum": ["simplex", "duplex_long_edge", "duplex_short_edge"]},
            "document_boundary": {
                "enum": ["continuous", "start_on_new_sheet", "start_on_recto"]
            },
        },
        ("version", "color_mode", "sides", "document_boundary"),
    ),
    "postal_mailpiece": _object(
        {
            "type": {"const": "letter"},
            "version": {"const": 1},
            "content": _object(
                {
                    "documents": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 25,
                        "items": {"$ref": "#/$defs/postal_document"},
                    }
                },
                ("documents",),
            ),
            "recipient": {"$ref": "#/$defs/postal_recipient"},
            "sender": {"$ref": "#/$defs/postal_sender"},
            "service": {"$ref": "#/$defs/postal_service"},
            "print": {"oneOf": [{"$ref": "#/$defs/postal_print"}, {"type": "null"}]},
            "references": {
                "type": "object",
                "maxProperties": 32,
                "propertyNames": {"pattern": "^[A-Za-z][A-Za-z0-9._-]{0,63}$"},
                "additionalProperties": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
            },
            "extensions": {
                "type": "object",
                "maxProperties": 32,
                "propertyNames": {
                    "pattern": "^[a-z][a-z0-9.-]{0,62}:[a-z][a-z0-9._-]{0,62}$"
                },
            },
        },
        ("type", "version", "content", "recipient", "sender", "service"),
    ),
}


def _source_property() -> dict[str, Any]:
    return {"$ref": "#/$defs/document_source"}


NODE_DETAILS: dict[str, dict[str, Any]] = {
    "archive": {
        "description": "Store complete immutable version snapshots in a configured archive.",
        "properties": {
            "documents": {
                "type": "array",
                "items": {"$ref": "#/$defs/archive_version"},
            },
            "destination": OPTIONAL_STRING,
        },
        "example": {
            "kind": "archive",
            "documents": [
                {
                    "metadata": {"invoice_number": "2026-0042"},
                    "representations": [
                        {
                            "name": "invoice.pdf",
                            "source": {
                                "source": "generate",
                                "id": "11111111-1111-1111-1111-111111111111",
                            },
                        }
                    ],
                }
            ],
        },
    },
    "bundle": {
        "description": "Create one ZIP archive from one to 25 document sources.",
        "properties": {
            "documents": {
                "type": "array",
                "minItems": 1,
                "maxItems": 25,
                "items": {"$ref": "#/$defs/bundle_node_document"},
            },
            "out": UUID,
            "compression": {"$ref": "#/$defs/compression"},
        },
        "required": ("documents", "out"),
        "example": {
            "kind": "bundle",
            "documents": [
                {
                    "source": "generate",
                    "id": "11111111-1111-1111-1111-111111111111",
                    "filename": "documents.pdf",
                }
            ],
            "out": "22222222-2222-2222-2222-222222222222",
        },
    },
    "debug": {
        "description": "Record a development diagnostic outcome without external delivery.",
        "properties": {},
        "example": {"kind": "debug"},
    },
    "doccle": {
        "description": "Publish one document to a Doccle receiver selected by ID or subject.",
        "properties": {
            "receiver": {
                "oneOf": [
                    _object({"id": NON_EMPTY_STRING}, ("id",)),
                    _object(
                        {"subject": {"type": "object", "minProperties": 1}},
                        ("subject",),
                    ),
                ]
            },
            "document": _source_property(),
            "document_type": NON_EMPTY_STRING,
            "name": {
                "type": ["object", "null"],
                "minProperties": 1,
                "additionalProperties": NON_EMPTY_STRING,
            },
            "published_at": {
                "type": ["string", "null"],
                "format": "date-time",
            },
        },
        "required": ("receiver", "document", "document_type"),
        "example": {
            "kind": "doccle",
            "receiver": {"id": "receiver-1"},
            "document": {
                "source": "generate",
                "id": "11111111-1111-1111-1111-111111111111",
            },
            "document_type": "invoice",
        },
    },
    "email": {
        "description": "Send an email body and optional generated-document attachments.",
        "properties": {
            "to": NON_EMPTY_STRING,
            "sender": NON_EMPTY_STRING,
            "body": _object(
                {
                    "plain": {"type": "string"},
                    "html": {"oneOf": [{"type": "string"}, _source_property()]},
                }
            ),
            "destination": OPTIONAL_STRING,
            "cc": {"type": "array", "items": NON_EMPTY_STRING},
            "bcc": {"type": "array", "items": NON_EMPTY_STRING},
            "attachments": {
                "type": "array",
                "items": _object(
                    {"id": UUID, "filename": OPTIONAL_STRING},
                    ("id", "filename"),
                ),
            },
            "reply_to": OPTIONAL_STRING,
            "subject": OPTIONAL_STRING,
        },
        "required": ("to", "sender", "body"),
        "example": {
            "kind": "email",
            "to": "recipient@example.test",
            "sender": "sender@example.test",
            "body": {"plain": "Hello"},
        },
    },
    "generate": {
        "description": "Materialize zero or more referenced, rendered, archived, or bundled documents.",
        "properties": {
            "documents": {"type": "array", "items": _source_property()},
        },
        "example": {"kind": "generate", "documents": []},
    },
    "peppol": {
        "description": "Submit one existing UBL document through the server-configured Peppol provider.",
        "properties": {"document": _source_property()},
        "required": ("document",),
        "example": {
            "kind": "peppol",
            "document": {
                "source": "generate",
                "id": "11111111-1111-1111-1111-111111111111",
            },
        },
    },
    "postal": {
        "description": "Produce and hand over a physical Belgian letter using a configured postal destination.",
        "properties": {
            "destination": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 128,
            },
            "mailpiece": {"$ref": "#/$defs/postal_mailpiece"},
        },
        "required": ("mailpiece",),
        "runtime_constraints": [
            "extensions must encode to at most 16384 bytes",
            "production availability and carrier products depend on the configured destination",
        ],
        "example": {
            "kind": "postal",
            "mailpiece": {
                "type": "letter",
                "version": 1,
                "content": {
                    "documents": [
                        {
                            "source": "generate",
                            "id": "11111111-1111-1111-1111-111111111111",
                        }
                    ]
                },
                "recipient": {
                    "name": "Jan Janssens",
                    "address": {
                        "format": "structured",
                        "street": "Wetstraat",
                        "house_number": "16",
                        "postal_code": "1000",
                        "city": "Brussel",
                        "country_code": "BE",
                    },
                },
                "sender": {"type": "profile", "id": "default"},
                "service": {
                    "type": "ordinary",
                    "version": 1,
                    "speed": "non_priority",
                },
            },
        },
    },
    "search-index": {
        "description": "Index exactly one immutable document source in a named search destination.",
        "properties": {
            "document": _source_property(),
            "destination": NON_EMPTY_STRING,
        },
        "required": ("document", "destination"),
        "example": {
            "kind": "search-index",
            "document": {
                "source": "generate",
                "id": "11111111-1111-1111-1111-111111111111",
            },
            "destination": "documents",
        },
    },
    "sftp": {
        "description": "Upload one document source to a relative path at a configured SFTP destination.",
        "properties": {
            "document": _source_property(),
            "path": {
                "type": "string",
                "minLength": 1,
                "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$)).+$",
            },
            "destination": OPTIONAL_STRING,
        },
        "required": ("document", "path"),
        "runtime_constraints": [
            "path is normalized and must not contain traversal or NUL characters"
        ],
        "example": {
            "kind": "sftp",
            "document": {
                "source": "generate",
                "id": "11111111-1111-1111-1111-111111111111",
            },
            "path": "out/document.pdf",
        },
    },
    "signature": {
        "description": "Create signed output documents from generated input document IDs.",
        "properties": {
            "documents": {
                "type": "array",
                "items": _object({"in": UUID, "out": UUID}, ("in", "out")),
            },
            "policy": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
            },
        },
        "required": ("documents",),
        "runtime_constraints": [
            "policy must exist in the deployment's signing policy registry"
        ],
        "example": {"kind": "signature", "documents": []},
    },
    "transform": {
        "description": "Convert, merge, or split existing document representations.",
        "properties": {
            "convert": _object(
                {
                    "document": _source_property(),
                    "out": UUID,
                    "content_type": {"const": "application/pdf"},
                },
                ("document", "out", "content_type"),
            ),
            "merge": _object(
                {
                    "documents": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 25,
                        "items": _source_property(),
                    },
                    "out": UUID,
                },
                ("documents", "out"),
            ),
            "split": _object(
                {
                    "document": _source_property(),
                    "outputs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 25,
                        "items": _object(
                            {
                                "pages": _object(
                                    {
                                        "start": {"type": "integer", "minimum": 1},
                                        "end": {"type": "integer", "minimum": 1},
                                    },
                                    ("start", "end"),
                                ),
                                "out": UUID,
                            },
                            ("pages", "out"),
                        ),
                    },
                },
                ("document", "outputs"),
            ),
        },
        "oneOf": [
            {"required": ["convert"]},
            {"required": ["merge"]},
            {"required": ["split"]},
        ],
        "runtime_constraints": [
            "split page ranges require start <= end",
            "merge and split accept PDF inputs only",
            "transform input and output bytes are bounded by deployment configuration",
        ],
        "example": {
            "kind": "transform",
            "convert": {
                "document": {
                    "source": "generate",
                    "id": "11111111-1111-1111-1111-111111111111",
                },
                "out": "22222222-2222-2222-2222-222222222222",
                "content_type": "application/pdf",
            },
        },
    },
    "wait-for": {
        "description": "Wait with configured backoffs for immutable archive documents to become available.",
        "properties": {
            "documents": {
                "type": "array",
                "items": _object(
                    {
                        "id": UUID,
                        "archive": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
                        },
                        "version": {"oneOf": [UUID, {"type": "null"}]},
                    },
                    ("id",),
                ),
            },
            "backoffs": {
                "type": ["array", "null"],
                "items": {"type": "number", "minimum": 0},
            },
        },
        "example": {"kind": "wait-for", "documents": [], "backoffs": [1, 2, 5]},
    },
    "webhook": {
        "description": "Send data or source-backed multipart fields to a public HTTP(S) endpoint.",
        "properties": {
            "url": {"type": "string", "format": "uri", "pattern": "^https?://"},
            "method": {"type": ["string", "null"], "default": "POST"},
            "data": {},
            "auth": {
                "oneOf": [
                    _object(
                        {
                            "basic": _object(
                                {
                                    "username": NON_EMPTY_STRING,
                                    "password": {"type": "string"},
                                },
                                ("username", "password"),
                            )
                        },
                        ("basic",),
                    ),
                    {"type": "null"},
                ]
            },
            "headers": {
                "type": ["object", "null"],
                "additionalProperties": {"type": "string"},
            },
            "multipart": {
                "type": ["array", "null"],
                "items": {
                    "oneOf": [
                        _object(
                            {
                                "name": NON_EMPTY_STRING,
                                "value": {},
                                "content_type": OPTIONAL_STRING,
                                "filename": OPTIONAL_STRING,
                            },
                            ("name",),
                        ),
                        _object(
                            {
                                "kind": {"const": "source"},
                                "name": NON_EMPTY_STRING,
                                "source": _source_property(),
                            },
                            ("kind", "name", "source"),
                        ),
                    ]
                },
            },
        },
        "required": ("url",),
        "runtime_constraints": [
            "the endpoint must resolve only to globally routable addresses",
            "redirects and URL credentials are rejected",
        ],
        "example": {"kind": "webhook", "url": "https://example.com/hook"},
    },
}


def capability_detail(kind: str) -> dict[str, Any]:
    try:
        return deepcopy(NODE_DETAILS[kind])
    except KeyError as ex:
        raise ValueError(f"Unknown node kind: {kind}") from ex


def flow_schema(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a recursive flow schema containing only deployed node kinds."""
    definitions = {
        **deepcopy(DOCUMENT_SOURCE_DEFINITIONS),
        **deepcopy(POSTAL_DEFINITIONS),
    }
    node_references = []
    for contract in contracts:
        kind = contract["kind"]
        detail = NODE_DETAILS[kind]
        outcomes = contract["outcomes"]
        properties = {
            "kind": {"const": kind},
            "id": {"oneOf": [UUID, {"type": "null"}]},
            **deepcopy(detail["properties"]),
            "on": {
                "type": ["object", "null"],
                "propertyNames": {"enum": outcomes},
                "additionalProperties": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/node"},
                },
            },
        }
        node_schema = _object(
            properties,
            ("kind", *detail.get("required", ())),
        )
        for keyword in ("oneOf", "anyOf", "allOf"):
            if keyword in detail:
                node_schema[keyword] = deepcopy(detail[keyword])
        definitions[f"node_{kind}"] = node_schema
        node_references.append({"$ref": f"#/$defs/node_{kind}"})
    definitions["node"] = {"oneOf": node_references}
    definitions["flow"] = _object(
        {
            "id": UUID,
            "correlation_id": UUID,
            "dag": {"$ref": "#/$defs/node"},
        },
        ("dag",),
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "#/$defs/flow",
        "$defs": definitions,
    }
