r"""Base blueprint definition of the v1 Email API."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.adapters import AdapterRegistry
from xarta.execution import ExecutionMode
from xarta.services.v1.email import utils
from xarta.services.v1.email.adapters import EmailAdapter
from xarta.services.v1.email.adapters import SMTPEmailAdapterFactory
from xarta.services.v1.email.api import email_operation_status
from xarta.services.v1.email.api import resend_callback
from xarta.services.v1.email.resend import ResendEmailAdapterFactory
from xarta.tracking import DestinationRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


@dataclass(frozen=True)
class EmailComponents:
    destinations: DestinationRegistry
    adapters: AdapterRegistry[EmailAdapter]
    execution_modes: Mapping[str, ExecutionMode] = field(
        default_factory=lambda: MappingProxyType({})
    )


def normalize_email_configurations(
    configurations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    normalized = deepcopy(dict(configurations))
    for configuration in normalized.values():
        if isinstance(configuration, dict):
            configuration.setdefault("kind", "email")
            configuration.setdefault("adapter", "smtp")
    return normalized


def build_email_components(
    configurations: Mapping[str, Mapping[str, Any]],
    adapters: AdapterRegistry[EmailAdapter] | None = None,
) -> EmailComponents:
    destinations = DestinationRegistry(
        normalize_email_configurations(configurations),
        require_explicit_revisions=True,
    )
    adapters = adapters or AdapterRegistry({"smtp": SMTPEmailAdapterFactory()})
    execution_modes = {}
    for resolved in destinations.iter_resolved():
        if resolved.binding.capability != "email":
            raise ValueError(
                f"Destination {resolved.binding.destination} is not valid for email"
            )
        adapters.validate(resolved.binding.adapter, resolved.configuration)
        adapter = adapters.create(resolved.binding.adapter, resolved.configuration)
        mode = adapter.execution_mode
        if not isinstance(mode, ExecutionMode):
            raise TypeError("Email adapter returned an invalid execution mode")
        previous = execution_modes.setdefault(resolved.binding.destination, mode)
        if previous is not mode:
            raise ValueError(
                f"Email destination {resolved.binding.destination} changes execution mode"
            )
    return EmailComponents(
        destinations=destinations,
        adapters=adapters,
        execution_modes=MappingProxyType(execution_modes),
    )


bp = Blueprint(name="email-v1", url_prefix="/api/v1/email")


env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "RENDER_SERVICE_ENDPOINT",
        "RENDER_SERVICE_TIMEOUT",
    },
)


@bp.listener("before_server_start")
async def _load_email_components(app: Sanic) -> None:
    if hasattr(app.ctx, "email_components"):
        return
    try:
        path = env.extract("EMAIL_CONFIGURATIONS_CONFIG_PATH") or env.extract(
            "SMTP_CONFIGURATIONS_CONFIG_PATH", default="config/smtp.json"
        )
        configurations = await utils.load_email_configurations(path)
        adapters: AdapterRegistry[EmailAdapter] = AdapterRegistry(
            {
                "smtp": SMTPEmailAdapterFactory(),
                "resend-rest": ResendEmailAdapterFactory(app.ctx.http_client_session),
            }
        )
        app.ctx.email_components = build_email_components(configurations, adapters)
    except (TypeError, ValueError) as ex:
        raise env.ConfigurationError(str(ex)) from ex


@bp.listener("before_server_start")
async def _setup_nats(app: Sanic) -> None:
    from xarta.services.v1.email.nats import EmailNATSModel

    await EmailNATSModel.register(app=app, components=app.ctx.email_components)


@bp.route("/", methods=["GET"])
async def email(_: Request) -> HTTPResponse:
    return response.empty()


bp.add_route(
    resend_callback,
    "/callbacks/resend/<destination:str>",
    methods=["POST"],
)
bp.add_route(
    email_operation_status,
    "/operations/<operation_id:str>",
    methods=["GET"],
)
