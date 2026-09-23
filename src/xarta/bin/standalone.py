r"""
Entrypoint that spins up all independent services.
The binary is mainly intended for development purposes or
low-traffic environments.
"""

from __future__ import annotations

import importlib
import pkgutil

from typing import TYPE_CHECKING

from sanic import response

import xarta.services.v1

from xarta.services.base import Service

from .utils import create_app

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request


app = create_app("Standalone")

path = xarta.services.v1.__path__
for _, module_name, is_pkg in pkgutil.iter_modules(path):
    if not is_pkg:
        continue
    service_path = f"xarta.services.v1.{module_name}"
    module = importlib.import_module(service_path)
    service: Service = module.service
    # Add all possible versions to the application.
    for version in service.versions:
        app.blueprint(service.blueprint(version=version))


@app.route("/.info/healthz", methods=["GET"])
async def healthz(_: Request) -> HTTPResponse:
    return response.empty()
