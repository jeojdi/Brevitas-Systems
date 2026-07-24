"""Provider-neutral managed-KMS boundary and a development/test-only adapter.

Production integrations implement :class:`ManagedKMS` with their cloud SDK.  This
module intentionally does not turn an environment-held symmetric key into a
"managed KMS".  ``LocalTestKMS`` is explicit, refuses production environments,
and exists only to make local development and deterministic tests safe.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KMSConfigurationError(RuntimeError):
    """A non-secret KMS configuration problem."""


class KMSUnavailable(RuntimeError):
    """The managed KMS could not complete an operation."""


_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+=,-]{0,511}$")
_KEY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_ALGORITHM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_AMBIGUOUS_VERSIONS = frozenset({
    "active", "aws_current", "awscurrent", "current", "default", "head", "latest",
    "live", "newest", "primary", "stable",
})


def _valid_key_version(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(
        _KEY_VERSION.fullmatch(value)
        and normalized not in _AMBIGUOUS_VERSIONS
        and not normalized.startswith("alias/")
        and not re.search(
            r"(?:^|[/:@._-])(?:latest|current|active|default|head|newest|primary|stable|live)$",
            normalized,
        )
    )


@dataclass(frozen=True)
class WrappedDataKey:
    """Opaque wrapped data key plus auditable, non-secret KMS metadata."""

    provider: str
    key_id: str
    key_version: str
    algorithm: str
    ciphertext: bytes


@runtime_checkable
class ManagedKMS(Protocol):
    """Minimal adapter contract implemented by a real managed-KMS client."""

    provider: str
    is_managed: bool

    def wrap_data_key(
        self,
        *,
        key_id: str,
        key_version: str,
        plaintext_key: bytes,
        encryption_context: Mapping[str, str],
    ) -> WrappedDataKey:
        """Encrypt a data key with a managed key; never log ``plaintext_key``."""

    def unwrap_data_key(
        self,
        wrapped_key: WrappedDataKey,
        *,
        encryption_context: Mapping[str, str],
    ) -> bytes:
        """Decrypt a wrapped data key; failures must not expose provider details."""


def is_production_environment(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    name = str(env.get("BREVITAS_ENV", "")).strip().lower()
    return name in {"prod", "production"} or any(
        env.get(name)
        for name in (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_PROJECT_ID",
        )
    )


def _metadata_aad(
    provider: str,
    key_id: str,
    key_version: str,
    algorithm: str,
    context: Mapping[str, str],
) -> bytes:
    context_fields = "\x1f".join(
        f"{key}={value}" for key, value in sorted(context.items())
    )
    return (
        f"provider={provider}\x1ekey_id={key_id}\x1eversion={key_version}"
        f"\x1ealgorithm={algorithm}\x1econtext={context_fields}"
    ).encode("utf-8")


class ExternalManagedKMS:
    """Hardened boundary around an actual cloud/HSM-backed adapter.

    The callbacks are supplied by deployment code using the selected provider's
    SDK and identity.  Callback exceptions are deliberately collapsed to stable,
    content-free errors so SDK messages cannot leak credentials into logs.
    """

    is_managed = True

    def __init__(
        self,
        provider: str,
        *,
        wrap: Callable[..., WrappedDataKey],
        unwrap: Callable[..., bytes],
    ) -> None:
        provider = provider.strip().lower()
        if (
            not _PROVIDER_NAME.fullmatch(provider)
            or provider in {"local", "local-test", "test"}
        ):
            raise KMSConfigurationError("managed KMS provider is invalid")
        self.provider = provider
        self._wrap = wrap
        self._unwrap = unwrap

    def wrap_data_key(
        self,
        *,
        key_id: str,
        key_version: str,
        plaintext_key: bytes,
        encryption_context: Mapping[str, str],
    ) -> WrappedDataKey:
        try:
            wrapped = self._wrap(
                key_id=key_id,
                key_version=key_version,
                plaintext_key=plaintext_key,
                encryption_context=dict(encryption_context),
            )
        except Exception:
            raise KMSUnavailable("managed KMS wrap operation unavailable") from None
        if not isinstance(wrapped, WrappedDataKey):
            raise KMSUnavailable("managed KMS returned an invalid wrapped key")
        return wrapped

    def unwrap_data_key(
        self,
        wrapped_key: WrappedDataKey,
        *,
        encryption_context: Mapping[str, str],
    ) -> bytes:
        try:
            plaintext = self._unwrap(
                wrapped_key=wrapped_key,
                encryption_context=dict(encryption_context),
            )
        except Exception:
            raise KMSUnavailable("managed KMS unwrap operation unavailable") from None
        if not isinstance(plaintext, bytes):
            raise KMSUnavailable("managed KMS returned an invalid data key")
        return plaintext


class LocalTestKMS:
    """AES-GCM key wrapper for local development and tests, never production."""

    provider = "local-test"
    is_managed = False
    algorithm = "LOCAL-TEST-AES-256-GCM"

    def __init__(
        self,
        master_key: bytes,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if is_production_environment(environ):
            raise KMSConfigurationError("local test KMS is forbidden in production")
        if not isinstance(master_key, bytes) or len(master_key) != 32:
            raise KMSConfigurationError("local test KMS requires a 256-bit key")
        self._cipher = AESGCM(master_key)

    def wrap_data_key(
        self,
        *,
        key_id: str,
        key_version: str,
        plaintext_key: bytes,
        encryption_context: Mapping[str, str],
    ) -> WrappedDataKey:
        nonce = os.urandom(12)
        aad = _metadata_aad(
            self.provider, key_id, key_version, self.algorithm, encryption_context
        )
        return WrappedDataKey(
            provider=self.provider,
            key_id=key_id,
            key_version=key_version,
            algorithm=self.algorithm,
            ciphertext=nonce + self._cipher.encrypt(nonce, plaintext_key, aad),
        )

    def unwrap_data_key(
        self,
        wrapped_key: WrappedDataKey,
        *,
        encryption_context: Mapping[str, str],
    ) -> bytes:
        if (
            wrapped_key.provider != self.provider
            or wrapped_key.algorithm != self.algorithm
            or len(wrapped_key.ciphertext) < 29
        ):
            raise KMSUnavailable("local test KMS wrapped key is invalid")
        nonce, ciphertext = wrapped_key.ciphertext[:12], wrapped_key.ciphertext[12:]
        aad = _metadata_aad(
            wrapped_key.provider,
            wrapped_key.key_id,
            wrapped_key.key_version,
            wrapped_key.algorithm,
            encryption_context,
        )
        try:
            return self._cipher.decrypt(nonce, ciphertext, aad)
        except Exception:
            raise KMSUnavailable("local test KMS unwrap operation failed") from None

    @classmethod
    def from_base64(
        cls,
        encoded: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "LocalTestKMS":
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise KMSConfigurationError("local test KMS key encoding is invalid") from None
        return cls(key, environ=environ)


@dataclass(frozen=True)
class KMSSettings:
    provider: str
    key_id: str
    key_version: str
    algorithm: str
    required: bool
    cache_ttl_seconds: float
    cache_max_entries: int

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "KMSSettings":
        env = os.environ if environ is None else environ
        required = is_production_environment(env) or str(
            env.get("BREVITAS_KMS_REQUIRED", "")
        ).strip().lower() in {"1", "true", "yes", "on"}

        try:
            ttl = float(env.get("BREVITAS_DATA_KEY_CACHE_TTL_SECONDS", "300"))
            maximum = int(env.get("BREVITAS_DATA_KEY_CACHE_MAX_ENTRIES", "256"))
        except (TypeError, ValueError):
            raise KMSConfigurationError("KMS cache bounds are invalid") from None
        if not 1 <= ttl <= 900 or not 1 <= maximum <= 1024:
            raise KMSConfigurationError("KMS cache bounds are outside safe limits")

        provider = str(env.get("BREVITAS_KMS_PROVIDER", "")).strip().lower()
        key_id = str(env.get("BREVITAS_KMS_KEY_ID", "")).strip()
        key_version = str(env.get("BREVITAS_KMS_KEY_VERSION", "")).strip()
        algorithm = str(
            env.get("BREVITAS_KMS_ALGORITHM", "provider-default")
        ).strip()
        if provider and not _PROVIDER_NAME.fullmatch(provider):
            raise KMSConfigurationError("managed KMS provider metadata is invalid")
        if key_id and not _KEY_ID.fullmatch(key_id):
            raise KMSConfigurationError("managed KMS key identity is invalid")
        if key_version and not _valid_key_version(key_version):
            raise KMSConfigurationError("managed KMS key version must be immutable")
        if not _ALGORITHM.fullmatch(algorithm or ""):
            raise KMSConfigurationError("managed KMS algorithm metadata is invalid")
        if required and (not provider or not key_id or not key_version):
            raise KMSConfigurationError("managed KMS configuration is incomplete")
        return cls(
            provider=provider,
            key_id=key_id,
            key_version=key_version,
            algorithm=algorithm or "provider-default",
            required=required,
            cache_ttl_seconds=ttl,
            cache_max_entries=maximum,
        )


def kms_from_environment(
    *,
    adapter: ManagedKMS | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ManagedKMS, KMSSettings]:
    """Resolve KMS configuration without ever inventing a production adapter."""

    env = os.environ if environ is None else environ
    settings = KMSSettings.from_environment(env)
    if adapter is not None:
        if settings.provider and adapter.provider != settings.provider:
            raise KMSConfigurationError("managed KMS provider does not match configuration")
        if settings.required and not adapter.is_managed:
            raise KMSConfigurationError("a managed KMS adapter is required")
        if not settings.key_id or not settings.key_version:
            raise KMSConfigurationError("KMS key identity is incomplete")
        return adapter, settings

    if settings.required:
        raise KMSConfigurationError("managed KMS adapter is unavailable")

    encoded = str(env.get("BREVITAS_LOCAL_KMS_KEY", "")).strip()
    if not encoded:
        raise KMSConfigurationError("explicit local test KMS configuration is required")
    local = LocalTestKMS.from_base64(encoded, environ=env)
    if settings.provider and settings.provider != local.provider:
        raise KMSConfigurationError("local KMS provider does not match configuration")
    if not settings.key_id or not settings.key_version:
        raise KMSConfigurationError("local KMS key identity is incomplete")
    return local, settings


__all__ = [
    "ExternalManagedKMS",
    "KMSConfigurationError",
    "KMSSettings",
    "KMSUnavailable",
    "LocalTestKMS",
    "ManagedKMS",
    "WrappedDataKey",
    "is_production_environment",
    "kms_from_environment",
]
