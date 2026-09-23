r"""
Eventing model and utilities for the email service.
"""

from __future__ import annotations

import asyncio
import datetime

from email.message import EmailMessage
from email.utils import getaddresses
from functools import partial
from typing import TYPE_CHECKING
from typing import cast

from xarta.exceptions.protocol import TemporaryError
from xarta.execution import ExecutionMode
from xarta.logging import log_capability_adapter_selected
from xarta.nats.sanic import SanicNATSRequestsConsumerModel
from xarta.protocol.dag import CapabilityResult
from xarta.protocol.dag import CommonOutcome
from xarta.protocol.dag import OutcomeEmission
from xarta.protocol.dag import OutcomeSubject
from xarta.protocol.dag.email import EmailNode
from xarta.services.v1.email.adapters import EmailCompletionMode
from xarta.services.v1.email.adapters import EmailSideEffectBoundary
from xarta.services.v1.email.adapters import classify_smtp_error
from xarta.services.v1.email.adapters import smtp_responses
from xarta.services.v1.email.tracking import AsyncEmailEventType
from xarta.services.v1.email.tracking import AsyncEmailUpdate
from xarta.services.v1.email.tracking import reduce_async_email
from xarta.services.v1.email.tracking import reduce_smtp_submission
from xarta.tracking import AmbiguousSubmissionError
from xarta.tracking import Reduction
from xarta.tracking import TrackedCapabilityService
from xarta.tracking import TrackedOperationLifecycle

if TYPE_CHECKING:
    from sanic import Sanic

    from xarta.protocol.dag.email import EmailAttachment
    from xarta.protocol.document.source import DocumentSource
    from xarta.protocol.document.source import DocumentSourceResult
    from xarta.services.v1.email.base import EmailComponents


_smtp_responses = smtp_responses
_classify_smtp_error = classify_smtp_error


async def _worker(
    node: EmailNode, task, *, components: EmailComponents, **kwargs
) -> CapabilityResult:
    assert isinstance(node, EmailNode)

    node.interpret()

    message = EmailMessage()
    message["Subject"] = node.subject
    message["From"] = node.sender
    message["To"] = node.to

    if node.bcc:
        message["Bcc"] = ", ".join(node.bcc)

    if node.cc:
        message["Cc"] = ", ".join(node.cc)

    if node.reply_to:
        message["Reply-To"] = node.reply_to

    if node.plain:
        message.set_content(node.plain)

    if node.html:
        source = cast("DocumentSource", node.html)
        source_result: DocumentSourceResult = await source.retrieve()
        message.add_alternative(source_result.data.decode(), "html")

    if node.attachments:
        attachments = cast("list[EmailAttachment]", node.attachments)
        # Asynchronously load all attachments.
        source_results = await asyncio.gather(
            *(attachment.retrieve() for attachment in attachments)
        )

        # Attach the document source results to the email.
        for source_result, source in zip(source_results, attachments, strict=True):
            main_type, sub_type = source_result.content_type.split("/", 1)
            message.add_attachment(
                source_result.data,
                maintype=main_type,
                subtype=sub_type,
                filename=source.filename,
            )

    recipients = _recipient_addresses([node.to, *node.cc, *node.bcc])
    sender_address = _sender_address(node.sender)
    destination = node.destination or sender_address
    resolved_destination = components.destinations.resolve("email", destination)
    selected_adapter = components.adapters.create(
        resolved_destination.binding.adapter, resolved_destination.configuration
    )
    log = kwargs.get("logger")
    if log is not None:
        await log_capability_adapter_selected(
            log, resolved_destination.binding, selected_adapter.execution_mode.value
        )
    initial_state = selected_adapter.initial_state(recipients)

    if selected_adapter.execution_mode is ExecutionMode.SYNCHRONOUS:
        plan = selected_adapter.plan_submission(
            initial_state,
            recipients,
            message,
            datetime.datetime.now(datetime.UTC),
        )

        async def before_submit() -> None:
            return None

        try:
            submission = await selected_adapter.submit(
                idempotency_key=str(task.node_execution_id),
                idempotency_valid_until=plan.idempotency_valid_until,
                sender=node.sender,
                recipients=recipients,
                message=message,
                before_submit=before_submit,
            )
        except AmbiguousSubmissionError:
            return CapabilityResult((OutcomeEmission(CommonOutcome.OUTCOME_UNCERTAIN),))
        if submission.completion is not EmailCompletionMode.IMMEDIATE:
            raise TypeError("Immediate email adapter requested feedback tracking")
        submission_outcomes = tuple(
            OutcomeEmission(
                outcome=value["outcome"],
                subject=(
                    OutcomeSubject.fromdict(value["subject"])
                    if value.get("subject")
                    else None
                ),
                details=value.get("details"),
            )
            for value in submission.state["submission_outcomes"]
        )
        reduction = reduce_smtp_submission(plan.state, submission_outcomes)
        return CapabilityResult(reduction.outcomes)

    service = TrackedCapabilityService(
        EmailNATSModel.tracking_store(),
        components.destinations,
        EmailNATSModel.publish,
    )
    existing_operation = await service.store.operation_for_execution(
        task.node_execution_id
    )
    resolved_destination = (
        components.destinations.resolve_binding(existing_operation.binding)
        if existing_operation is not None
        else resolved_destination
    )
    selected_adapter = components.adapters.create(
        resolved_destination.binding.adapter, resolved_destination.configuration
    )
    if selected_adapter.execution_mode is not ExecutionMode.TRACKED:
        raise TypeError("Tracked email operation requires a feedback adapter")
    initial_state = selected_adapter.initial_state(recipients)

    async def submit(operation, configuration):
        plan = selected_adapter.plan_submission(
            operation.state,
            recipients,
            message,
            datetime.datetime.now(datetime.UTC),
        )
        if dict(operation.state) != dict(plan.state):
            operation = await service.checkpoint_operation(operation, plan.state)

        async def mark_side_effect_uncertain() -> None:
            if (
                plan.side_effect_boundary
                is EmailSideEffectBoundary.CHECKPOINTED_IDEMPOTENCY
            ):
                return
            try:
                await service.mark_uncertain(operation.id, source="submission")
            except Exception as ex:
                raise TemporaryError(
                    "Could not persist SMTP submission uncertainty", delay=1
                ) from ex

        try:
            submission = await selected_adapter.submit(
                idempotency_key=str(operation.id),
                idempotency_valid_until=plan.idempotency_valid_until,
                sender=node.sender,
                recipients=recipients,
                message=message,
                before_submit=mark_side_effect_uncertain,
            )
        except TemporaryError:
            # An explicit temporary refusal proves the message was not accepted.
            if plan.side_effect_boundary is EmailSideEffectBoundary.MARK_UNCERTAIN:
                await service.mark_waiting(
                    operation.id, None, plan.state, source="submission"
                )
            raise
        return submission.provider_reference, {
            **plan.state,
            **submission.state,
            "completion": submission.completion.value,
        }

    operation = await service.start(
        task,
        "email",
        destination,
        initial_state,
        submit,
        provider_account_reference=selected_adapter.provider_account_reference,
    )
    if operation.lifecycle is TrackedOperationLifecycle.UNCERTAIN:
        await service.feedback(
            operation.id,
            operation.binding.adapter,
            f"{operation.binding.adapter}-submission-uncertain",
            None,
            lambda state, update: Reduction(
                state=state,
                outcomes=(OutcomeEmission(CommonOutcome.OUTCOME_UNCERTAIN),),
                operation_resolved=True,
            ),
            "submission",
        )
        return CapabilityResult(outcomes_persisted=True)

    if operation.state.get("completion") == EmailCompletionMode.FEEDBACK.value:
        async_recipients = tuple(operation.state["recipients"])
        await service.feedback(
            operation.id,
            operation.binding.adapter,
            f"{operation.binding.adapter}-submission-accepted",
            AsyncEmailUpdate(AsyncEmailEventType.ACCEPTED, async_recipients),
            reduce_async_email,
            "submission",
        )
        return CapabilityResult(outcomes_persisted=True, waiting_feedback=True)

    submission_outcomes = tuple(
        OutcomeEmission(
            outcome=value["outcome"],
            subject=(
                OutcomeSubject.fromdict(value["subject"])
                if value.get("subject")
                else None
            ),
            details=value.get("details"),
        )
        for value in operation.state["submission_outcomes"]
    )

    await service.feedback(
        operation.id,
        operation.binding.adapter,
        f"{operation.binding.adapter}-submission-accepted",
        None,
        lambda state, update: reduce_smtp_submission(state, submission_outcomes),
        "submission",
    )
    return CapabilityResult(outcomes_persisted=True)


class EmailNATSModel(SanicNATSRequestsConsumerModel):
    @staticmethod
    def _resolve_task_mode(task, components: EmailComponents) -> ExecutionMode:
        node = task.node
        if not isinstance(node, EmailNode):
            raise TypeError("Email consumer received a non-email task")
        destination = node.destination or _sender_address(node.sender)
        resolved = components.destinations.resolve("email", destination)
        return components.execution_modes[resolved.binding.destination]

    @classmethod
    async def register(  # type: ignore[override]
        cls, app: Sanic, *, components: EmailComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name="email",
            execution_mode_resolver=partial(
                cls._resolve_task_mode, components=components
            ),
            **kwargs,
        )


def _recipient_addresses(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            address.casefold() for _, address in getaddresses(values) if address
        )
    )


def _sender_address(value: str) -> str:
    addresses = _recipient_addresses([value])
    if len(addresses) != 1:
        raise ValueError("Email sender must contain exactly one address")
    return addresses[0]
