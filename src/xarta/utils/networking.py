r"""
Networking-related utilities.
"""

from __future__ import annotations

import ipaddress


def is_bogon(ip: str) -> bool:
    r"""
    Returns true whenever the specified ip-address is a bogon address (private).
    """
    return ipaddress.ip_address(ip).is_private
