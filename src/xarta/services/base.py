r"""
Base utilities representing a service, commonly used for managing
service versions and blueprint groups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sanic import Blueprint


class Service:

    def __init__(
        self,
        name: str,
        **kwargs,
    ):
        self._blueprints = kwargs
        self._name = name

    def blueprint(self, version: str = "latest") -> Blueprint:
        return self._blueprints[version]

    @property
    def name(self) -> str:
        return self._name

    @property
    def versions(self) -> Iterable[str]:
        return self._blueprints.keys()

    @property
    def blueprints(self) -> Iterable[Blueprint]:
        return self._blueprints.values()
