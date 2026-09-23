r"""
Eventing model and utilities for the digital signature service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xarta.crypto.sign.policy import ExistingSignatureMismatch
from xarta.crypto.sign.policy import SignatureError
from xarta.exceptions.protocol import PermanentError
from xarta.nats.sanic import SanicNATSSynchronousRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag.signature import SignatureNode
from xarta.protocol.document.source import DocumentSourceResult
from xarta.protocol.document.source import GenerateDocumentSource

if TYPE_CHECKING:
    from sanic import Sanic

    from xarta.crypto.sign.policy import SignaturePolicy
    from xarta.services.v1.signature.adapters import SignatureComponents


async def _sign(
    request,
    components: SignatureComponents,
    policy: SignaturePolicy,
) -> None:
    source = await request.source.retrieve()
    try:
        existing = await GenerateDocumentSource(request.output_id).retrieve()
        if not await components.pdf_validator.matches_existing_output(
            source=source.data,
            signed=existing.data,
            policy=policy,
        ):
            raise ExistingSignatureMismatch(
                "Existing signature output does not match its source"
            )
        return
    except FileNotFoundError:
        pass

    signed = await components.pdf_signer.sign(source.data, policy=policy)
    try:
        await DocumentSourceResult(
            id=request.output_id,
            document_type=source.document_type,
            content_type="application/pdf",
            metadata=source.metadata,
            data=signed,
        ).persist()
    except FileExistsError:
        # A concurrent delivery may have won the immutable output key.
        existing = await GenerateDocumentSource(request.output_id).retrieve()
        if not await components.pdf_validator.matches_existing_output(
            source=source.data,
            signed=existing.data,
            policy=policy,
        ):
            raise ExistingSignatureMismatch(
                "Concurrent signature output does not match its source"
            )


async def _worker(
    node: SignatureNode,
    app: Sanic | None = None,
    components: SignatureComponents | None = None,
    **kwargs,
) -> CapabilityResult:
    assert isinstance(node, SignatureNode)

    requests = node.interpret()
    if requests:
        if components is None:
            if app is None:
                raise RuntimeError("Signature components are not available")
            components = app.ctx.signature_components
        for request in requests:
            try:
                if request.policy is None and app is not None:
                    app.ctx.logger.warning(
                        "signature_policy_unbound",
                        signature_policy_unbound=True,
                    )
                policy = components.policies.resolve(
                    request.policy,
                    default_policy=components.default_policy_id,
                    allow_development_policies=components.allow_development_policies,
                )
                await _sign(request, components, policy)
            except SignatureError as ex:
                raise PermanentError(
                    str(ex),
                    classification="signature_policy_or_output",
                    error_code="signature_request_rejected",
                ) from ex

    return CapabilityResult((OutcomeEmission(outcome="success"),))


class SignatureNATSModel(SanicNATSSynchronousRequestsConsumerModel):
    @classmethod
    async def register(cls, app: Sanic, **kwargs) -> None:  # type: ignore[override]
        await super().register(
            app=app,
            fn=_worker,
            name="signature",
            **kwargs,
        )
