r"""
Collection of generic (base) utilities used throughout the module.
"""

from __future__ import annotations

import datetime


def now(tz: datetime.tzinfo = datetime.UTC) -> datetime.datetime:
    r"""
    A utility method that generates a timezone-based timestamp
    of the current time. By default, the timestamp will be
    configured within the UTC timezone.
    """
    return datetime.datetime.now(tz=tz)
