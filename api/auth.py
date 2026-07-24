import secrets
import hashlib
import re


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def generate_api_key() -> str:
    return "bvt_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def verify_key_hash(key: str, expected_hash: str) -> bool:
    """Compare an API key hash in constant time, including malformed inputs.

    The dummy comparison keeps invalid stored values on the same digest/compare
    path.  Callers should return one generic authentication failure for all false
    results and must never log ``key``.
    """

    actual = hash_key(key if isinstance(key, str) else "")
    expected = expected_hash.lower() if isinstance(expected_hash, str) else ""
    valid = bool(_SHA256_HEX.fullmatch(expected))
    candidate = expected if valid else ("0" * 64)
    matches = secrets.compare_digest(actual, candidate)
    return valid and matches


def constant_time_equal(left: str | bytes, right: str | bytes) -> bool:
    """Bounded-type wrapper for comparing internal bearer/service credentials."""

    if type(left) is not type(right) or not isinstance(left, (str, bytes)):
        # Execute a fixed dummy comparison even for invalid caller input.
        secrets.compare_digest(b"\0" * 32, b"\1" * 32)
        return False
    return secrets.compare_digest(left, right)


__all__ = ["constant_time_equal", "generate_api_key", "hash_key", "verify_key_hash"]
