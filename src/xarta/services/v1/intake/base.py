r"""
Base blueprint definition of the v1 intake API.
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import orjson

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.crypto.sign.configuration import default_signature_configuration
from xarta.crypto.sign.configuration import load_signature_configuration
from xarta.crypto.sign.configuration import parse_allow_development_policies
from xarta.crypto.sign.policy import ELECTRONIC_SEAL_V1
from xarta.crypto.sign.policy import SignatureError
from xarta.crypto.sign.policy import validate_policy_id
from xarta.exceptions.http import BadRequestError
from xarta.exceptions.http import ServiceUnavailable
from xarta.flow_profiles import FlowProfileError
from xarta.flow_profiles import FlowProfileReference
from xarta.flow_profiles import FlowProfileRegistry
from xarta.flow_profiles import compile_flow_profile
from xarta.flow_profiles import load_flow_profiles
from xarta.flow_profiles import profile_validation_values
from xarta.payments.x402 import InvalidPaymentChallengeError
from xarta.payments.x402 import InvalidSettlementEvidenceError
from xarta.payments.x402 import MalformedPaymentHeaderError
from xarta.payments.x402 import PaymentRequirementsMismatchError
from xarta.payments.x402 import PaymentSettlement
from xarta.payments.x402 import X402HTTPTransport
from xarta.payments.x402 import X402PaymentServer
from xarta.payments.x402 import parse_x402_configuration
from xarta.pricing import load_pricing_configuration

from .capabilities import CapabilityManifest
from .capabilities import UnsupportedCapabilitiesError
from .nats import IntakeNATSModel as NATS
from .preparation import IntakeUpload
from .preparation import IntakeValidationError
from .preparation import PreparedIntake
from .preparation import prepare_intake

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


bp: Blueprint = Blueprint(
    name="intake-v1",
    url_prefix="/api/v1/intake",
)


env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
        "INTAKE_CAPABILITIES",
    },
)


async def commit_intake(
    prepared: PreparedIntake, admission: dict[str, str] | None = None
) -> UUID:
    """Persist uploaded sources and publish a prepared flow to JetStream."""
    if prepared.sources:
        await asyncio.gather(*(source.persist() for source in prepared.sources))
    try:
        await NATS.schedule(prepared.flow, admission=admission)
    except Exception as ex:
        raise ServiceUnavailable(
            "The flow could not be accepted because messaging is unavailable."
        ) from ex
    return prepared.flow.id


def _prepare(
    request: Request,
    definition: dict | bytes,
    *,
    uploads: tuple[IntakeUpload, ...] = (),
) -> PreparedIntake:
    return prepare_intake(
        definition,
        uploads=uploads,
        capabilities=request.app.ctx.intake_capabilities,
        profiles=getattr(request.app.ctx, "flow_profiles", FlowProfileRegistry.empty()),
        default_signature_policy=getattr(
            request.app.ctx,
            "signature_default_policy",
            ELECTRONIC_SEAL_V1.id,
        ),
        signature_policies=getattr(request.app.ctx, "signature_policies", None),
        allow_development_policies=getattr(
            request.app.ctx, "signature_allow_development_policies", True
        ),
    )


def _pricing_summary(request: Request, prepared: PreparedIntake) -> dict:
    configuration = getattr(request.app.ctx, "x402_configuration", None)
    if configuration is None or not configuration.protects("intake"):
        return {"enabled": False}
    quote = request.app.ctx.pricing_engine.quote_flow(
        prepared.flow.dag,
        resource=configuration.intake_resource_url,
        request_fingerprint=prepared.semantic_fingerprint,
    )
    return {
        "enabled": True,
        "pricing_revision": quote.pricing_revision,
        "selling_price": {
            "amount": str(quote.selling_price.amount),
            "currency": quote.selling_price.currency,
        },
        "expires_at": quote.expires_at.isoformat(),
    }


def _boolean_environment(name: str, default: bool = False) -> bool:
    raw = str(env.extract(name, default="true" if default else "false", dtype=str))
    if raw.lower() not in {"true", "false"}:
        raise env.ConfigurationError(f"{name} must be true or false")
    return raw.lower() == "true"


def _x402_environment_configuration():
    enabled = _boolean_environment("X402_ENABLED")
    value = {
        "enabled": enabled,
        "intake_enabled": _boolean_environment("X402_INTAKE_ENABLED"),
        "preview_enabled": _boolean_environment("X402_PREVIEW_ENABLED"),
    }
    if enabled:
        value.update(
            {
                "facilitator_url": env.extract(
                    "X402_FACILITATOR_URL", optional=False, dtype=str
                ),
                "network": env.extract("X402_NETWORK", optional=False, dtype=str),
                "pay_to": env.extract("X402_PAY_TO", optional=False, dtype=str),
                "payment_timeout_seconds": env.extract(
                    "X402_PAYMENT_TIMEOUT_SECONDS", optional=False, dtype=int
                ),
                "intake_resource_url": env.extract(
                    "X402_INTAKE_RESOURCE_URL", optional=False, dtype=str
                ),
                "preview_resource_url": env.extract(
                    "X402_PREVIEW_RESOURCE_URL", dtype=str
                ),
                "challenge_signing_key": env.extract(
                    "X402_CHALLENGE_SIGNING_KEY",
                    optional=False,
                    dtype=str,
                ),
            }
        )
    return parse_x402_configuration(value)


async def _issue_payment_challenge(
    request: Request, prepared: PreparedIntake
) -> HTTPResponse | None:
    configuration = request.app.ctx.x402_configuration
    quote = request.app.ctx.pricing_engine.quote_flow(
        prepared.flow.dag,
        resource=configuration.intake_resource_url,
        request_fingerprint=prepared.semantic_fingerprint,
    )
    if quote.selling_price.amount == Decimal(0):
        return None
    if not prepared.flow_id_supplied:
        raise BadRequestError(
            "Paid intake requires an explicit flow id so payment retries are stable."
        )
    payment_required, requirements = (
        request.app.ctx.x402_payment_server.issue_intake_payment_required(
            quote,
            description="Paid asynchronous document intake",
            mime_type="application/json",
        )
    )
    return response.json(
        {"error": "Payment is required for this intake."},
        status=402,
        headers={
            "PAYMENT-REQUIRED": request.app.ctx.x402_transport.payment_required_header(
                payment_required
            )
        },
    )


async def _accept_intake(
    request: Request, prepared: PreparedIntake, successful_response: dict
) -> HTTPResponse:
    configuration = getattr(request.app.ctx, "x402_configuration", None)
    if configuration is None or not configuration.protects("intake"):
        flow_id = await commit_intake(prepared)
        return response.json({**successful_response, "id": str(flow_id)}, status=202)

    try:
        payment_payload = request.app.ctx.x402_transport.read_payment_payload(
            request.headers
        )
    except MalformedPaymentHeaderError as ex:
        raise BadRequestError(str(ex)) from ex
    if payment_payload is None:
        challenge = await _issue_payment_challenge(request, prepared)
        if challenge is not None:
            return challenge
        flow_id = await commit_intake(prepared)
        return response.json({**successful_response, "id": str(flow_id)}, status=202)
    if not prepared.flow_id_supplied:
        raise BadRequestError(
            "Paid intake requires an explicit flow id so payment retries are stable."
        )

    try:
        request.app.ctx.x402_payment_server.require_payment_identifier(payment_payload)
        requirements = (
            request.app.ctx.x402_payment_server.require_authenticated_requirements(
                payment_payload,
                resource=configuration.intake_resource_url,
                request_fingerprint=prepared.semantic_fingerprint,
            )
        )
    except (
        InvalidPaymentChallengeError,
        PaymentRequirementsMismatchError,
        ValueError,
    ) as ex:
        raise BadRequestError(str(ex)) from ex
    if (requirements.extra or {}).get("paymentFlow") != "upfront":
        raise BadRequestError("Paid intake requires the x402 upfront payment flow.")
    try:
        settlement_response = await request.app.ctx.x402_payment_server.settle(
            payment_payload,
            requirements,
            declared_extensions=request.app.ctx.x402_payment_server.payment_extensions(),
        )
    except Exception as ex:
        raise ServiceUnavailable(
            "The x402 settlement result is uncertain; no workflow was started."
        ) from ex
    if not settlement_response.success:
        return response.json({"error": "x402 settlement was rejected"}, status=402)
    try:
        settlement = PaymentSettlement.from_response(settlement_response, requirements)
    except InvalidSettlementEvidenceError as ex:
        raise ServiceUnavailable(
            "The facilitator returned incomplete settlement evidence; no workflow was started."
        ) from ex
    request.app.ctx.logger.info(
        "x402_settlement_succeeded",
        **settlement.dict(),
        resource="intake",
        flow_id=str(prepared.flow.id),
        request_fingerprint=prepared.semantic_fingerprint,
    )
    await commit_intake(prepared, admission={"type": "x402", **settlement.dict()})
    return response.json(
        {**successful_response, "payment": settlement.dict()},
        status=200,
        headers={
            "PAYMENT-RESPONSE": request.app.ctx.x402_transport.payment_response_header(
                settlement_response
            )
        },
    )


def _request_uploads(request: Request) -> tuple[bytes | dict, tuple[IntakeUpload, ...]]:
    if not request.files:
        return request.json, ()

    flow_files = request.files.getlist("flow")
    if len(flow_files) != 1:
        raise BadRequestError("A multipart request requires exactly one flow file.")

    uploads = []
    for name, files in request.files.items():
        if name == "flow":
            continue
        if len(files) != 1:
            raise BadRequestError(
                f"Multipart field {name} must contain exactly one document."
            )
        uploaded = files[0]
        uploads.append(
            IntakeUpload(
                name=name,
                content_type=uploaded.type,
                body=uploaded.body,
            )
        )
    return flow_files[0].body, tuple(uploads)


@bp.route("/", methods=["POST"])
async def intake(request: Request) -> HTTPResponse:
    flow_definition, uploads = _request_uploads(request)
    try:
        prepared = _prepare(request, flow_definition, uploads=uploads)
    except (IntakeValidationError, UnsupportedCapabilitiesError) as ex:
        raise BadRequestError(str(ex)) from ex
    return await _accept_intake(request, prepared, {"id": str(prepared.flow.id)})


@bp.get("/capabilities")
async def intake_capabilities(request: Request) -> HTTPResponse:
    manifest = request.app.ctx.intake_capabilities
    configuration = getattr(request.app.ctx, "x402_configuration", None)
    return response.json(
        {
            "capabilities": manifest.contracts(),
            "flow_schema": manifest.schema(),
            "graph": {
                "root": "dag",
                "common_fields": {
                    "kind": "deployed capability name",
                    "id": "optional UUID",
                    "on": "optional outcome-to-node-list mapping",
                },
            },
            "pricing": {
                "enabled": bool(
                    configuration is not None and configuration.protects("intake")
                )
            },
        }
    )


@bp.post("/prepare")
async def prepare_flow(request: Request) -> HTTPResponse:
    if request.files:
        raise BadRequestError("Flow preparation accepts application/json only.")
    try:
        prepared = _prepare(request, request.json)
    except (IntakeValidationError, UnsupportedCapabilitiesError) as ex:
        raise BadRequestError(str(ex)) from ex
    return response.raw(
        orjson.dumps(
            {
                "flow": prepared.staging_dict(),
                "pricing": _pricing_summary(request, prepared),
            },
            default=str,
        ),
        content_type="application/json",
    )


@bp.get("/profiles")
async def flow_profile_contracts(request: Request) -> HTTPResponse:
    return response.json({"profiles": request.app.ctx.flow_profiles.contracts()})


@bp.get("/profiles/<name:str>/<version:int>")
async def flow_profile_contract(
    request: Request, name: str, version: int
) -> HTTPResponse:
    try:
        reference = FlowProfileReference(name, version)
        profile = request.app.ctx.flow_profiles.resolve(reference)
    except FlowProfileError as ex:
        return response.json({"error": str(ex)}, status=404)
    contract = next(
        item
        for item in request.app.ctx.flow_profiles.contracts()
        if item["delivery_profile"] == str(profile.reference)
    )
    return response.json(contract)


@bp.post("/profiles/<name:str>/<version:int>")
async def submit_flow_profile(
    request: Request, name: str, version: int
) -> HTTPResponse:
    if request.files:
        raise BadRequestError(
            "Profile-specific intake currently accepts application/json only."
        )
    payload = request.json
    if not isinstance(payload, dict):
        raise BadRequestError("A profile submission must be a JSON object.")
    unknown = set(payload) - {"id", "correlation_id", "inputs"}
    if unknown:
        raise BadRequestError(
            f"Unsupported profile submission fields: {', '.join(sorted(unknown))}"
        )
    definition = {
        key: value for key, value in payload.items() if key in {"id", "correlation_id"}
    }
    definition["delivery_profile"] = f"{name}@{version}"
    definition["delivery_values"] = payload.get("inputs", {})
    try:
        prepared = _prepare(request, definition)
    except (IntakeValidationError, UnsupportedCapabilitiesError) as ex:
        raise BadRequestError(str(ex)) from ex
    return await _accept_intake(
        request,
        prepared,
        {
            "id": str(prepared.flow.id),
            "delivery_profile": str(prepared.profile_reference),
            "semantic_fingerprint": prepared.semantic_fingerprint,
        },
    )


@bp.listener("before_server_start")
async def _setup_capabilities(app: Sanic) -> None:
    raw = env.extract("INTAKE_CAPABILITIES", optional=False)
    try:
        configuration_path = env.extract("SIGNATURE_CONFIG_PATH", dtype=str)
        signature_configuration = (
            load_signature_configuration(configuration_path)
            if configuration_path
            else default_signature_configuration()
        )
        configured_signature_policy = env.extract("SIGNATURE_DEFAULT_POLICY", dtype=str)
        signature_default_policy = validate_policy_id(
            configured_signature_policy or signature_configuration.default_policy_id
        )
        allow_development = parse_allow_development_policies(
            env.extract(
                "SIGNATURE_ALLOW_DEVELOPMENT_POLICIES",
                default="true",
                dtype=str,
            )
        )
        signature_configuration.policies.resolve(
            signature_default_policy,
            default_policy=signature_default_policy,
            allow_development_policies=allow_development,
        )
        app.ctx.signature_default_policy = signature_default_policy
        app.ctx.signature_policies = signature_configuration.policies
        app.ctx.signature_allow_development_policies = allow_development
        if configuration_path is None and hasattr(app.ctx, "logger"):
            app.ctx.logger.warning(
                "signature_configuration_implicit",
                policy_id=ELECTRONIC_SEAL_V1.id,
            )
        app.ctx.intake_capabilities = CapabilityManifest.from_json(raw)
        path = env.extract("FLOW_PROFILES_CONFIG_PATH")
        app.ctx.flow_profiles = (
            load_flow_profiles(path) if path else FlowProfileRegistry.empty()
        )
        validation_flow_id = UUID("00000000-0000-0000-0000-000000000004")
        for profile in app.ctx.flow_profiles.profiles.values():
            dag = compile_flow_profile(
                profile,
                flow_id=validation_flow_id,
                values=profile_validation_values(profile),
            )
            app.ctx.intake_capabilities.require_supported(dag)
    except (SignatureError, ValueError) as ex:
        raise env.ConfigurationError(str(ex)) from ex


@bp.listener("before_server_start")
async def _setup_runtime(app: Sanic) -> None:
    """Connect the required intake messaging runtime."""
    await NATS.register(app=app)


@bp.listener("before_server_start")
async def _setup_payments(app: Sanic) -> None:
    try:
        configuration = _x402_environment_configuration()
        app.ctx.x402_configuration = configuration
        if not configuration.protects("intake"):
            return
        pricing_path = env.extract("PRICING_CONFIG_PATH", optional=False, dtype=str)
        catalog, resolver = load_pricing_configuration(pricing_path)
        from xarta.pricing import PricingEngine

        app.ctx.pricing_engine = PricingEngine(catalog, resolver)
        payment_server = X402PaymentServer(configuration)
        payment_server.initialize()
        app.ctx.x402_payment_server = payment_server
        app.ctx.x402_transport = X402HTTPTransport()
    except (TypeError, ValueError) as ex:
        raise env.ConfigurationError(str(ex)) from ex


@bp.listener("before_server_stop")
async def _close_payment_server(app: Sanic) -> None:
    server = getattr(app.ctx, "x402_payment_server", None)
    if server is not None:
        await server.aclose()
