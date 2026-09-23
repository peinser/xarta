r"""
Uncategorized generic cryptographic utilities.
"""

from __future__ import annotations

import random
import secrets

import bcrypt


def generate_password(n: int = 16) -> tuple[str, bytes]:
    r"""
    A method responsible for securely generating a secret and a `bcrypt` hash
    of the generate secret. An optional parameter `n` can be specified, which
    is the number of hexadecimal characters that the secret will contain.
    """
    secret = secrets.token_hex(nbytes=n)
    hashed = bcrypt.hashpw(secret.encode(), bcrypt.gensalt())

    return secret, hashed


def otp(digits: int = 6) -> str:
    r"""
    Returns an One-Time Password with the specified number
    of digits, whose default is 6.
    """
    assert digits >= 0

    x = random.randint(0, 10**6 - 1)
    return str(x).zfill(digits)
