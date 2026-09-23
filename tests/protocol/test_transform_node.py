from __future__ import annotations

from uuid import uuid4

import pytest

from jsonschema import Draft202012Validator

import xarta.protocol.dag

from xarta.protocol.dag.transform import TransformConvert
from xarta.protocol.dag.transform import TransformMerge
from xarta.protocol.dag.transform import TransformNode
from xarta.protocol.dag.transform import TransformSplit
from xarta.services.v1.intake.capabilities import CapabilityManifest


def generated(identifier=None) -> dict:
    return {"source": "generate", "id": str(identifier or uuid4())}


@pytest.mark.parametrize(
    "specification, operation_type",
    [
        (
            {
                "kind": "transform",
                "convert": {
                    "document": generated(),
                    "out": str(uuid4()),
                    "content_type": "application/pdf",
                },
            },
            TransformConvert,
        ),
        (
            {
                "kind": "transform",
                "merge": {
                    "documents": [generated(), generated()],
                    "out": str(uuid4()),
                },
            },
            TransformMerge,
        ),
        (
            {
                "kind": "transform",
                "split": {
                    "document": generated(),
                    "outputs": [
                        {"pages": {"start": 1, "end": 3}, "out": str(uuid4())},
                        {"pages": {"start": 4, "end": 4}, "out": str(uuid4())},
                    ],
                },
            },
            TransformSplit,
        ),
    ],
)
def test_transform_node_round_trip(specification: dict, operation_type) -> None:
    node = xarta.protocol.dag.parse(specification)
    restored = xarta.protocol.dag.parse(node.dict())

    assert isinstance(node, TransformNode)
    assert isinstance(restored, TransformNode)
    assert isinstance(restored.interpret(), operation_type)
    assert (
        restored.dict()[operation_type.__name__.removeprefix("Transform").lower()]
        == specification[operation_type.__name__.removeprefix("Transform").lower()]
    )


def test_transform_requires_exactly_one_operation() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        TransformNode()
    with pytest.raises(ValueError, match="exactly one"):
        TransformNode(
            convert={
                "document": generated(),
                "out": str(uuid4()),
                "content_type": "application/pdf",
            },
            merge={"documents": [generated(), generated()], "out": str(uuid4())},
        )


def test_transform_schema_requires_exactly_one_operation() -> None:
    schema = CapabilityManifest(frozenset({"transform"})).schema()
    validator = Draft202012Validator(schema)
    valid = {
        "dag": {
            "kind": "transform",
            "convert": {
                "document": generated(),
                "out": str(uuid4()),
                "content_type": "application/pdf",
            },
        }
    }
    assert list(validator.iter_errors(valid)) == []

    missing = {"dag": {"kind": "transform"}}
    duplicate = {
        "dag": {
            "kind": "transform",
            "convert": valid["dag"]["convert"],
            "merge": {"documents": [generated(), generated()], "out": str(uuid4())},
        }
    }
    assert list(validator.iter_errors(missing))
    assert list(validator.iter_errors(duplicate))


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (
            {"merge": {"documents": [generated()], "out": str(uuid4())}},
            "between 2 and 25",
        ),
        (
            {
                "split": {
                    "document": generated(),
                    "outputs": [
                        {
                            "pages": {"start": 0, "end": 1},
                            "out": str(uuid4()),
                        }
                    ],
                }
            },
            "positive start",
        ),
        (
            {
                "split": {
                    "document": generated(),
                    "outputs": [
                        {
                            "pages": {"start": 3, "end": 2},
                            "out": str(uuid4()),
                        }
                    ],
                }
            },
            "positive start",
        ),
    ],
)
def test_transform_rejects_invalid_bounds(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TransformNode(**kwargs)


def test_transform_split_output_ids_are_unique() -> None:
    output = uuid4()
    with pytest.raises(ValueError, match="unique"):
        TransformNode(
            split={
                "document": generated(),
                "outputs": [
                    {"pages": {"start": 1, "end": 1}, "out": str(output)},
                    {"pages": {"start": 2, "end": 2}, "out": str(output)},
                ],
            }
        )


def test_transform_output_cannot_overwrite_generated_input() -> None:
    identifier = uuid4()
    with pytest.raises(ValueError, match="overwrite"):
        TransformNode(
            convert={
                "document": generated(identifier),
                "out": str(identifier),
                "content_type": "application/pdf",
            }
        )
