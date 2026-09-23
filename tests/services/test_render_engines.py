from __future__ import annotations

import json

from typing import TYPE_CHECKING

import xarta.services.v1.render.utils as utils_module

from xarta.protocol.template.engine import GotenbergTemplateEngine
from xarta.protocol.template.engine import JinjaTemplateEngine
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEngineKind

if TYPE_CHECKING:
    from pathlib import Path


async def test_load_template_engines_builds_every_supported_kind(
    tmp_path: Path, monkeypatch
) -> None:
    configuration = tmp_path / "template-engines.json"
    configuration.write_text(
        json.dumps(
            {
                "jinja": {
                    "endpoint": "http://jinja:8000",
                    "timeout": 10.0,
                    "kind": "jinja",
                },
                "gotenberg": {
                    "endpoint": "http://gotenberg:3000",
                    "timeout": 30.0,
                    "kind": "gotenberg",
                },
            }
        )
    )
    monkeypatch.setattr(
        utils_module, "TEMPLATE_ENGINES_CONFIG_PATH", str(configuration)
    )

    engines = await utils_module.load_template_engines()

    jinja = engines[TemplateEngineIdentifier("jinja")]
    gotenberg = engines[TemplateEngineIdentifier("gotenberg")]

    assert isinstance(jinja, JinjaTemplateEngine)
    assert isinstance(gotenberg, GotenbergTemplateEngine)
    assert gotenberg.type is TemplateEngineKind.GOTENBERG
    assert gotenberg.endpoint == "http://gotenberg:3000"
    assert gotenberg.timeout == 30.0
