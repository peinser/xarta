from __future__ import annotations

import importlib

from xarta.services.base import Service


def load(service: str) -> Service:
    r"""
    Attemts to load the dependencies and versions of the specified service.

    For now, we support the following modules:
    expire_archived_documents.service.name: expire_archived_documents.service,
    setup_nats.service.name: setup_nats.service,
    """
    module = importlib.import_module(f"xarta.jobs.{service}")
    return module.service
