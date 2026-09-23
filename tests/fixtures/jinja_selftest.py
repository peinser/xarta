from __future__ import annotations

SELFTEST_TEMPLATE_ENGINE = "jinja"
SELFTEST_TEMPLATE_PATH = "engine/selftest.html"

SELFTEST_DEFAULT_MARKERS = (
    "Engine self-test",
    "2 februari 2020",
    "01:30:15",
    "1234,56",
    "Operationeel",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
)


def jinja_selftest_template_engine_options() -> dict[str, object]:
    return {
        SELFTEST_TEMPLATE_ENGINE: {
            "kind": SELFTEST_TEMPLATE_ENGINE,
            "template": {"path": SELFTEST_TEMPLATE_PATH},
        }
    }
