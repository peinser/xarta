r"""
Cryptographic hashing utilities.
"""

from __future__ import annotations

import hashlib


def _hash(hasher: hashlib._Hash, payload: bytes) -> bytes:
    hasher.update(payload)

    return hasher.digest()


def _hash_hex(hasher: hashlib._Hash, payload: bytes) -> str:
    hasher.update(payload)

    return hasher.hexdigest()


def sha256(payload: bytes, serialize: bool = False) -> bytes | tuple[bytes, str]:
    hasher = hashlib.sha256()
    hasher.update(payload)

    if serialize:
        return hasher.digest(), hasher.hexdigest()

    return hasher.digest()


def sha512(payload: bytes, serialize: bool = False) -> bytes | tuple[bytes, str]:
    hasher = hashlib.sha512()
    hasher.update(payload)

    if serialize:
        return hasher.digest(), hasher.hexdigest()

    return hasher.digest()


def hex_sha256(payload: bytes) -> str:
    return _hash_hex(hasher=hashlib.sha256(), payload=payload)


def hex_sha512(payload: bytes) -> str:
    return _hash_hex(hasher=hashlib.sha512(), payload=payload)


def sha3_256(payload: bytes) -> bytes:
    return _hash(hashlib.sha3_256(), payload=payload)


def sha3_512(payload: bytes) -> bytes:
    return _hash(hashlib.sha3_512(), payload=payload)


def hash(payload: bytes) -> bytes:
    r"""
    Generic hashing utility which uses the sha512 hash of the pipeline
    as a salt that will be fed into a sha3_256 hash.
    """
    salt = sha512(payload=payload)
    return sha3_256(salt + payload)
