import secrets
import hashlib


def generate_api_key() -> str:
    return "bvt_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
