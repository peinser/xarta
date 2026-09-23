from __future__ import annotations

from xarta import json


def test_json_round_trip() -> None:
    value = {"enabled": True, "count": 3}

    assert json.loads(json.dumps(value)) == value
