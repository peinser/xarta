r"""
Rendering service generic utilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiofiles

from xarta import env
from xarta import json
from xarta.protocol.template.engine import GotenbergTemplateEngine
from xarta.protocol.template.engine import JinjaTemplateEngine
from xarta.protocol.template.engine import ScripturaTemplateEngine
from xarta.protocol.template.engine import TemplateEngine
from xarta.protocol.template.engine import TemplateEngineIdentifier
from xarta.protocol.template.engine import TemplateEngineKind

if TYPE_CHECKING:
    from typing import Final


TEMPLATE_ENGINES_CONFIG_PATH: Final[str] = env.extract(
    key="TEMPLATE_ENGINES_CONFIG_PATH",
    default="config/template-engines.json",
    dtype=str,
)


async def load_template_engines() -> dict[TemplateEngineIdentifier, TemplateEngine]:
    async with aiofiles.open(TEMPLATE_ENGINES_CONFIG_PATH) as f:
        raw = json.loads(await f.read())

    configuration = {}

    for k, raw_options in raw.items():
        options = dict(raw_options)
        engine_type = TemplateEngineKind(options.pop("kind"))

        engine_identifier = TemplateEngineIdentifier(value=k)

        match engine_type:
            case TemplateEngineKind.JINJA:
                cls = JinjaTemplateEngine
            case TemplateEngineKind.SCRIPTURA:
                cls = ScripturaTemplateEngine
            case TemplateEngineKind.GOTENBERG:
                cls = GotenbergTemplateEngine
            case _:
                raise ValueError("Unknown template engine kind provided", engine_type)

        engine = cls(identifier=engine_identifier, **options)

        configuration[engine_identifier] = engine

    return configuration
