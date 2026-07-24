"""Google Cloud KMS adapter for the managed envelope-encryption boundary.

The adapter uses Application Default Credentials.  Runtime identity is therefore
deployment-owned: prefer Workload Identity Federation, and never place a raw KMS
key in application configuration.  Provider exceptions are deliberately
collapsed to the repository's content-free error types.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Mapping

from .kms import (
    KMSConfigurationError,
    KMSUnavailable,
    WrappedDataKey,
)


PROVIDER = "google-cloud-kms"
ALGORITHM = "GOOGLE_SYMMETRIC_ENCRYPTION"

_CRYPTO_KEY_RESOURCE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/"
    r"[a-z0-9-]{1,63}/keyRings/[A-Za-z0-9_-]{1,63}/"
    r"cryptoKeys/[A-Za-z0-9_-]{1,63}$"
)
_KEY_VERSION = re.compile(r"^[1-9][0-9]{0,18}$")


def _context_aad(context: Mapping[str, str]) -> bytes:
    if not isinstance(context, Mapping) or len(context) > 16:
        raise KMSConfigurationError("Google Cloud KMS context is invalid")
    normalized: dict[str, str] = {}
    for key, value in context.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise KMSConfigurationError("Google Cloud KMS context is invalid")
        if not key or len(key) > 64 or len(value) > 512:
            raise KMSConfigurationError("Google Cloud KMS context is invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
            raise KMSConfigurationError("Google Cloud KMS context is invalid")
        normalized[key] = value
    return json.dumps(
        dict(sorted(normalized.items())),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _version_resource(key_id: str, key_version: str) -> str:
    if not _CRYPTO_KEY_RESOURCE.fullmatch(str(key_id or "")):
        raise KMSConfigurationError("Google Cloud KMS key resource is invalid")
    if not _KEY_VERSION.fullmatch(str(key_version or "")):
        raise KMSConfigurationError("Google Cloud KMS key version is invalid")
    return f"{key_id}/cryptoKeyVersions/{key_version}"


class GoogleCloudKMS:
    """Managed-KMS implementation backed by a symmetric Google Cloud key."""

    provider = PROVIDER
    is_managed = True
    algorithm = ALGORITHM

    def __init__(
        self,
        client: object,
        *,
        crc32c: Callable[[bytes], int],
        timeout_seconds: float = 0.75,
    ) -> None:
        if client is None or not callable(getattr(client, "encrypt", None)):
            raise KMSConfigurationError("Google Cloud KMS client is invalid")
        if not callable(getattr(client, "decrypt", None)) or not callable(crc32c):
            raise KMSConfigurationError("Google Cloud KMS client is invalid")
        if not 0.05 <= float(timeout_seconds) <= 10:
            raise KMSConfigurationError("Google Cloud KMS timeout is invalid")
        self._client = client
        self._crc32c = crc32c
        self._timeout_seconds = float(timeout_seconds)

    def _checksum(self, value: bytes) -> int:
        checksum = self._crc32c(value)
        if not isinstance(checksum, int) or not 0 <= checksum <= 0xFFFFFFFF:
            raise KMSUnavailable("Google Cloud KMS integrity check unavailable")
        return checksum

    def wrap_data_key(
        self,
        *,
        key_id: str,
        key_version: str,
        plaintext_key: bytes,
        encryption_context: Mapping[str, str],
    ) -> WrappedDataKey:
        version_resource = _version_resource(key_id, key_version)
        if not isinstance(plaintext_key, bytes) or len(plaintext_key) != 32:
            raise KMSConfigurationError("Google Cloud KMS data key is invalid")
        aad = _context_aad(encryption_context)
        try:
            response = self._client.encrypt(
                request={
                    "name": version_resource,
                    "plaintext": plaintext_key,
                    "additional_authenticated_data": aad,
                    "plaintext_crc32c": self._checksum(plaintext_key),
                    "additional_authenticated_data_crc32c": self._checksum(aad),
                },
                retry=None,
                timeout=self._timeout_seconds,
            )
            ciphertext = response.ciphertext
            if (
                response.name != version_resource
                or response.verified_plaintext_crc32c is not True
                or response.verified_additional_authenticated_data_crc32c is not True
                or not isinstance(ciphertext, bytes)
                or not ciphertext
                or response.ciphertext_crc32c != self._checksum(ciphertext)
            ):
                raise KMSUnavailable("Google Cloud KMS wrap integrity check failed")
        except KMSUnavailable:
            raise
        except Exception:
            raise KMSUnavailable("Google Cloud KMS wrap operation unavailable") from None
        return WrappedDataKey(
            provider=self.provider,
            key_id=key_id,
            key_version=key_version,
            algorithm=self.algorithm,
            ciphertext=ciphertext,
        )

    def unwrap_data_key(
        self,
        wrapped_key: WrappedDataKey,
        *,
        encryption_context: Mapping[str, str],
    ) -> bytes:
        if (
            not isinstance(wrapped_key, WrappedDataKey)
            or wrapped_key.provider != self.provider
            or wrapped_key.algorithm != self.algorithm
            or not isinstance(wrapped_key.ciphertext, bytes)
            or not wrapped_key.ciphertext
        ):
            raise KMSConfigurationError("Google Cloud KMS wrapped key is invalid")
        _version_resource(wrapped_key.key_id, wrapped_key.key_version)
        aad = _context_aad(encryption_context)
        try:
            response = self._client.decrypt(
                request={
                    "name": wrapped_key.key_id,
                    "ciphertext": wrapped_key.ciphertext,
                    "additional_authenticated_data": aad,
                    "ciphertext_crc32c": self._checksum(wrapped_key.ciphertext),
                    "additional_authenticated_data_crc32c": self._checksum(aad),
                },
                retry=None,
                timeout=self._timeout_seconds,
            )
            plaintext = response.plaintext
            if (
                not isinstance(plaintext, bytes)
                or len(plaintext) != 32
                or response.plaintext_crc32c != self._checksum(plaintext)
            ):
                raise KMSUnavailable("Google Cloud KMS unwrap integrity check failed")
        except KMSUnavailable:
            raise
        except Exception:
            raise KMSUnavailable("Google Cloud KMS unwrap operation unavailable") from None
        return plaintext


def create_adapter() -> GoogleCloudKMS:
    """Build the production adapter with Google Application Default Credentials."""

    provider = os.getenv("BREVITAS_KMS_PROVIDER", "").strip().lower()
    if provider and provider != PROVIDER:
        raise KMSConfigurationError("Google Cloud KMS provider configuration is invalid")
    try:
        timeout = float(os.getenv("BREVITAS_GCP_KMS_TIMEOUT_SECONDS", "0.75"))
    except (TypeError, ValueError):
        raise KMSConfigurationError("Google Cloud KMS timeout is invalid") from None
    try:
        import google_crc32c
        from google.cloud import kms_v1

        client = kms_v1.KeyManagementServiceClient()
    except Exception:
        raise KMSConfigurationError(
            "Google Cloud KMS client initialization failed"
        ) from None
    return GoogleCloudKMS(
        client,
        crc32c=google_crc32c.value,
        timeout_seconds=timeout,
    )


__all__ = [
    "ALGORITHM",
    "GoogleCloudKMS",
    "PROVIDER",
    "create_adapter",
]
