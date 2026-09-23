from __future__ import annotations

from xarta.services.v1.doccle.models import CallbackResult
from xarta.services.v1.doccle.models import DoccleAdapter
from xarta.services.v1.doccle.models import DoccleDocument
from xarta.services.v1.doccle.models import DoccleReceiver
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResult
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import DoccleTransportCategory
from xarta.services.v1.doccle.models import ReceiverCallback
from xarta.services.v1.doccle.models import ReceiverState
from xarta.services.v1.doccle.repositories import ReceiverRepository

__all__ = [
    "CallbackResult",
    "DoccleAdapter",
    "DoccleDocument",
    "DoccleReceiver",
    "DoccleReceiverProfile",
    "DoccleResult",
    "DoccleResultCategory",
    "DoccleTransportCategory",
    "ReceiverCallback",
    "ReceiverRepository",
    "ReceiverState",
    "service",
]


def __getattr__(name: str):
    # Keep model/adapter imports independent from Sanic bootstrap and DAG loading.
    if name == "service":
        from xarta.services.base import Service
        from xarta.services.v1.doccle.base import bp

        return Service(
            name="doccle",
            v1=bp,
            latest=bp.copy("doccle-latest", url_prefix="/api/doccle"),
        )
    raise AttributeError(name)
