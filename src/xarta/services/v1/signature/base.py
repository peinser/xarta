r"""
Base blueprint definition of the v1 Signature API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sanic import Blueprint
from sanic import response

from xarta import env
from xarta.crypto.sign.configuration import default_signature_configuration
from xarta.crypto.sign.configuration import load_signature_configuration
from xarta.crypto.sign.configuration import parse_allow_development_policies
from xarta.crypto.sign.policy import ELECTRONIC_SEAL_V1
from xarta.crypto.sign.policy import SignatureError
from xarta.crypto.sign.timestamp import load_timestamp_provider_configurations
from xarta.services.v1.signature.adapters import create_signature_components
from xarta.storage.configuration import initialize_temporary_storage

from .nats import SignatureNATSModel as NATS

if TYPE_CHECKING:
    from sanic import HTTPResponse
    from sanic import Request
    from sanic import Sanic


bp: Blueprint = Blueprint(
    name="signature-v1",
    url_prefix="/api/v1/signature",
)


env.verify(
    blueprint=bp,
    required={
        "NATS_SERVERS",
        "TMP_STORAGE",
    },
)


@bp.listener("before_server_start")
async def _setup_signature(app: Sanic) -> None:
    r"""
    A method which configures the connection with NATS and the Jetstream component.

    Note: additions to @bp.listener will be executed sequently (unless an await is present).
    """
    if not hasattr(app.ctx, "signature_components"):
        configuration_path = env.extract("SIGNATURE_CONFIG_PATH", dtype=str)
        configured_default = env.extract("SIGNATURE_DEFAULT_POLICY", dtype=str)
        if configuration_path is None and hasattr(app.ctx, "logger"):
            app.ctx.logger.warning(
                "signature_configuration_implicit",
                policy_id=ELECTRONIC_SEAL_V1.id,
            )
        allow_development = env.extract(
            "SIGNATURE_ALLOW_DEVELOPMENT_POLICIES",
            default="true",
            dtype=str,
        )
        try:
            signature_configuration = (
                load_signature_configuration(configuration_path)
                if configuration_path
                else default_signature_configuration()
            )
            timestamp_configuration_path = env.extract(
                "TIMESTAMP_PROVIDERS_CONFIG_PATH", dtype=str
            )
            timestamp_provider_configurations = (
                load_timestamp_provider_configurations(timestamp_configuration_path)
                if timestamp_configuration_path
                else {}
            )
            configuration = {
                "adapter": env.extract("SIGNATURE_ADAPTER", default="local", dtype=str),
                "private_pem": env.extract("DOCUMENT_SIGN_PRIVATE_PEM", dtype=str),
                "public_pem": env.extract("DOCUMENT_SIGN_PUBLIC_PEM", dtype=str),
                "chain_pem": env.extract("DOCUMENT_SIGN_CHAIN_PEM", dtype=str),
                "signature_configuration": signature_configuration,
                "timestamp_provider_configurations": (
                    timestamp_provider_configurations
                ),
                "default_policy": (
                    configured_default or signature_configuration.default_policy_id
                ),
                "allow_development_policies": parse_allow_development_policies(
                    allow_development
                ),
            }
            app.ctx.signature_components = create_signature_components(configuration)
            await initialize_temporary_storage()
        except (SignatureError, ValueError) as ex:
            raise env.ConfigurationError(str(ex)) from ex

    await NATS.register(app=app)


@bp.route("/", methods=["GET"])
async def debug(_: Request) -> HTTPResponse:
    return response.empty()


@bp.route("/", methods=["POST"])
async def test(request: Request) -> HTTPResponse:
    # Check whether the binary is provided through an HTTP MultiPart request.
    if request.files:
        first_file_key = next(iter(request.files))
        first_file = request.files[first_file_key][0]
        data = first_file.body

    else:
        data = request.body

    return response.raw(
        await request.app.ctx.signature_components.pdf_signer.sign(
            data,
            policy=request.app.ctx.signature_components.policies.resolve(
                None,
                default_policy=(request.app.ctx.signature_components.default_policy_id),
                allow_development_policies=(
                    request.app.ctx.signature_components.allow_development_policies
                ),
            ),
        ),
        content_type="application/pdf",
        status=200,
    )
