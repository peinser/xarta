from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from xarta.execution import ExecutionMode
from xarta.logging import log_capability_adapter_selected
from xarta.nats.sanic import SanicNATSRequestsConsumerModel
from xarta.protocol.dag.postal import PostalNode

if TYPE_CHECKING:
    from sanic import Sanic

    from xarta.protocol.dag import NodeTask
    from xarta.services.v1.postal.adapters import PostalComponents


async def _worker(
    node: PostalNode,
    task: NodeTask,
    *,
    components: PostalComponents,
    logger,
    **kwargs,
):
    del kwargs
    if not isinstance(node, PostalNode):
        raise TypeError("Postal consumer received a non-postal task")
    request = node.interpret()
    existing = await PostalNATSModel.tracking_store().operation_for_execution(
        task.node_execution_id
    )
    resolved = (
        components.destinations.resolve_binding(existing.binding)
        if existing is not None
        else components.destinations.resolve("postal", request.destination)
    )
    adapter = components.adapters.create(
        resolved.binding.adapter, resolved.configuration
    )
    if (
        adapter.execution_mode
        is not components.execution_modes[resolved.binding.destination]
    ):
        raise TypeError("Postal destination execution mode changed at runtime")
    log = logger.bind(
        adapter=resolved.binding.adapter,
        configuration_revision=resolved.binding.configuration_revision,
        execution_mode=adapter.execution_mode.value,
    )
    await log_capability_adapter_selected(
        log, resolved.binding, adapter.execution_mode.value
    )
    return await adapter.submit(task=task, request=request, logger=log)


class PostalNATSModel(SanicNATSRequestsConsumerModel):
    @staticmethod
    def _resolve_task_mode(
        task: NodeTask, components: PostalComponents
    ) -> ExecutionMode:
        node = task.node
        if not isinstance(node, PostalNode):
            raise TypeError("Postal consumer received a non-postal task")
        resolved = components.destinations.resolve("postal", node.destination)
        return components.execution_modes[resolved.binding.destination]

    @classmethod
    async def register(  # type: ignore[override]
        cls, app: Sanic, *, components: PostalComponents, **kwargs
    ) -> None:
        await super().register(
            app=app,
            fn=partial(_worker, components=components),
            name="postal",
            execution_mode_resolver=partial(
                cls._resolve_task_mode, components=components
            ),
            **kwargs,
        )
