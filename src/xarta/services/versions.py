from __future__ import annotations

import importlib

from .base import Service


def load(service: str) -> Service:
    r"""
    Attemts to load the dependencies and versions of the specified service.

    For now, we support the following modules:
    # from .v1 import archive
    # from .v1 import intake
    # from .v1 import documenttype
    # from .v1 import render
    # from .v1 import generate
    # from .v1 import preview
    # from .v1 import webhook
    # from .v1 import debug
    # from .v1 import email
    # from .v1 import signature
    """
    module = importlib.import_module(f"xarta.services.v1.{service}")
    return module.service
