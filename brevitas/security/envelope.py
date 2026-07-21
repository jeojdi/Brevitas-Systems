"""Versioned AES-GCM envelope encryption with bounded data-key caching."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from .kms import (
    KMSConfigurationError,
    KMSUnavailable,
    ManagedKMS,
    WrappedDataKey,
    kms_from_environment,
)


ENVELOPE_PREFIX = "bvt-envelope:v1:"
ENVELOPE_SCHEMA = "brevitas.envelope"
ENVELOPE_VERSION = 1
DATA_ALGORITHM = "AES-256-GCM"
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
MAX_WRAPPED_KEY_BYTES = 64 * 1024
MAX_PLAINTEXT_BYTES = 1024 * 1024
_SAFE_WRAP_ALGORITHM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


class EnvelopeError(RuntimeError):
    """Base class with deliberately content-free messages."""


class EnvelopeFormatError(EnvelopeError):
    pass


class EnvelopeDecryptionError(EnvelopeError):
    pass


class LegacyDecryptor(Protocol):
    def decrypt(self, ciphertext: str) -> bytes:
        """Decrypt an explicitly supported legacy ciphertext or fail closed."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: object, *, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum * 2:
        raise EnvelopeFormatError("credential envelope encoding is invalid")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise EnvelopeFormatError("credential envelope encoding is invalid") from None
    if len(decoded) > maximum:
        raise EnvelopeFormatError("credential envelope exceeds its size limit")
    return decoded


def _bounded_metadata(value: object, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise EnvelopeFormatError(f"credential envelope {name} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EnvelopeFormatError(f"credential envelope {name} is invalid")
    return value


def _normalize_context(context: Mapping[str, str] | None) -> dict[str, str]:
    if context is None:
        return {}
    if not isinstance(context, Mapping) or len(context) > 16:
        raise EnvelopeFormatError("credential encryption context is invalid")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in context.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise EnvelopeFormatError("credential encryption context is invalid")
        if not raw_key or len(raw_key) > 64 or len(raw_value) > 512:
            raise EnvelopeFormatError("credential encryption context is invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_key + raw_value):
            raise EnvelopeFormatError("credential encryption context is invalid")
        normalized[raw_key] = raw_value
    return normalized


def _context_digest(context: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(sorted(context.items())), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _aad(metadata: Mapping[str, object]) -> bytes:
    return json.dumps(
        metadata, separators=(",", ":"), sort_keys=True, ensure_ascii=True
    ).encode("utf-8")


class BoundedTTLKeyCache:
    """Thread-safe TTL/LRU cache that wipes bytearrays on eviction."""

    HARD_MAX_ENTRIES = 1024
    HARD_MAX_TTL_SECONDS = 900.0

    def __init__(
        self,
        *,
        max_entries: int = 256,
        ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= int(max_entries) <= self.HARD_MAX_ENTRIES:
            raise ValueError("data-key cache maximum is outside safe limits")
        if not 1 <= float(ttl_seconds) <= self.HARD_MAX_TTL_SECONDS:
            raise ValueError("data-key cache TTL is outside safe limits")
        self.max_entries = int(max_entries)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._values: OrderedDict[str, tuple[float, bytearray]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _wipe(value: bytearray) -> None:
        for index in range(len(value)):
            value[index] = 0

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, (deadline, _) in self._values.items() if deadline <= now]
        for key in expired:
            _, value = self._values.pop(key)
            self._wipe(value)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            self._purge_expired(self._clock())
            item = self._values.pop(key, None)
            if item is None:
                return None
            self._values[key] = item
            return bytes(item[1])

    def put(self, key: str, value: bytes) -> None:
        if not isinstance(value, bytes) or len(value) != 32:
            raise ValueError("only 256-bit data keys may be cached")
        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            existing = self._values.pop(key, None)
            if existing:
                self._wipe(existing[1])
            self._values[key] = (now + self.ttl_seconds, bytearray(value))
            while len(self._values) > self.max_entries:
                _, (_, evicted) = self._values.popitem(last=False)
                self._wipe(evicted)

    def clear(self) -> None:
        with self._lock:
            for _, value in self._values.values():
                self._wipe(value)
            self._values.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired(self._clock())
            return len(self._values)


class LegacyFernetDecryptor:
    """Explicit, decrypt-only bridge for pre-envelope Fernet values."""

    def __init__(self, keys: Iterable[str | bytes]) -> None:
        ciphers = []
        for value in keys:
            try:
                ciphers.append(Fernet(value.encode() if isinstance(value, str) else value))
            except (TypeError, ValueError):
                raise KMSConfigurationError("legacy credential key is invalid") from None
        if not ciphers:
            raise KMSConfigurationError("at least one legacy credential key is required")
        self._ciphers = tuple(ciphers)

    def decrypt(self, ciphertext: str) -> bytes:
        encoded = ciphertext.encode("utf-8")
        for cipher in self._ciphers:
            try:
                return cipher.decrypt(encoded)
            except InvalidToken:
                continue
        raise EnvelopeDecryptionError("legacy credential cannot be decrypted")


@dataclass(frozen=True)
class DecryptionResult:
    plaintext: bytes
    source: str
    key_id: str
    key_version: str
    needs_rotation: bool


@dataclass(frozen=True)
class RotationRecord:
    ciphertext: str
    context: Mapping[str, str]


@dataclass(frozen=True)
class RotationSummary:
    inspected: int
    reencrypted: int
    already_current: int
    failed: int
    dry_run: bool


class EnvelopeCipher:
    """Encrypt credentials under one active managed key version."""

    def __init__(
        self,
        kms: ManagedKMS,
        *,
        key_id: str,
        key_version: str,
        wrap_algorithm: str = "provider-default",
        legacy_decryptor: LegacyDecryptor | None = None,
        cache: BoundedTTLKeyCache | None = None,
    ) -> None:
        if not isinstance(kms, ManagedKMS):
            raise KMSConfigurationError("KMS adapter does not satisfy the required interface")
        self.kms = kms
        self.key_id = _bounded_metadata(key_id, "key id")
        self.key_version = _bounded_metadata(key_version, "key version", 128)
        self.wrap_algorithm = _bounded_metadata(wrap_algorithm, "wrap algorithm", 128)
        self.legacy_decryptor = legacy_decryptor
        self.cache = cache if cache is not None else BoundedTTLKeyCache()

    def _wrapped_key_is_current(self, wrapped: object) -> bool:
        return bool(
            isinstance(wrapped, WrappedDataKey)
            and wrapped.provider == self.kms.provider
            and wrapped.key_id == self.key_id
            and wrapped.key_version == self.key_version
            and (
                self.wrap_algorithm == "provider-default"
                or wrapped.algorithm == self.wrap_algorithm
            )
            and isinstance(wrapped.algorithm, str)
            and _SAFE_WRAP_ALGORITHM.fullmatch(wrapped.algorithm)
            and isinstance(wrapped.ciphertext, bytes)
            and wrapped.ciphertext
            and len(wrapped.ciphertext) <= MAX_WRAPPED_KEY_BYTES
        )

    def probe_kms(self) -> None:
        """Prove the configured KMS can wrap and unwrap one ephemeral data key.

        The random probe key and its wrapped form exist only in process memory. The
        fixed context contains no tenant, credential, or customer data, and the
        mutable plaintext buffers are wiped on a best-effort basis before returning.
        """
        context = {
            "purpose": "kms-readiness",
            "schema": "brevitas.kms-readiness.v1",
        }
        data_key = bytearray(os.urandom(32))
        unwrapped = bytearray()
        try:
            try:
                wrapped = self.kms.wrap_data_key(
                    key_id=self.key_id,
                    key_version=self.key_version,
                    plaintext_key=bytes(data_key),
                    encryption_context=context,
                )
            except KMSUnavailable:
                raise
            except Exception:
                raise KMSUnavailable(
                    "managed KMS readiness wrap unavailable") from None
            if not self._wrapped_key_is_current(wrapped):
                raise KMSUnavailable(
                    "managed KMS readiness returned inconsistent key metadata")
            try:
                plaintext = self.kms.unwrap_data_key(
                    wrapped, encryption_context=context
                )
            except KMSUnavailable:
                raise
            except Exception:
                raise KMSUnavailable(
                    "managed KMS readiness unwrap unavailable") from None
            if not isinstance(plaintext, bytes) or len(plaintext) != 32:
                raise KMSUnavailable(
                    "managed KMS readiness returned an invalid data key")
            unwrapped.extend(plaintext)
            if not hmac.compare_digest(data_key, unwrapped):
                raise KMSUnavailable("managed KMS readiness round trip failed")
        finally:
            BoundedTTLKeyCache._wipe(data_key)
            BoundedTTLKeyCache._wipe(unwrapped)

    @staticmethod
    def is_envelope(ciphertext: object) -> bool:
        return isinstance(ciphertext, str) and ciphertext.startswith(ENVELOPE_PREFIX)

    @staticmethod
    def _metadata(
        wrapped: WrappedDataKey,
        *,
        context_digest: str,
    ) -> dict[str, object]:
        return {
            "schema": ENVELOPE_SCHEMA,
            "version": ENVELOPE_VERSION,
            "key_provider": wrapped.provider,
            "key_id": wrapped.key_id,
            "key_version": wrapped.key_version,
            "wrap_algorithm": wrapped.algorithm,
            "data_algorithm": DATA_ALGORITHM,
            "context_digest": context_digest,
        }

    def encrypt_bytes(
        self, plaintext: bytes, *, context: Mapping[str, str] | None = None
    ) -> str:
        if not isinstance(plaintext, bytes):
            raise TypeError("credential plaintext must be bytes")
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise EnvelopeFormatError("credential plaintext exceeds its size limit")
        normalized_context = _normalize_context(context)
        data_key = bytearray(os.urandom(32))
        try:
            try:
                wrapped = self.kms.wrap_data_key(
                    key_id=self.key_id,
                    key_version=self.key_version,
                    plaintext_key=bytes(data_key),
                    encryption_context=normalized_context,
                )
            except KMSUnavailable:
                raise
            except Exception:
                raise KMSUnavailable("managed KMS wrap operation unavailable") from None
            if not self._wrapped_key_is_current(wrapped):
                raise KMSUnavailable("managed KMS returned inconsistent key metadata")
            _bounded_metadata(wrapped.provider, "key provider", 128)
            _bounded_metadata(wrapped.algorithm, "wrap algorithm", 128)
            metadata = self._metadata(
                wrapped, context_digest=_context_digest(normalized_context)
            )
            nonce = os.urandom(12)
            encrypted = AESGCM(bytes(data_key)).encrypt(nonce, plaintext, _aad(metadata))
            document = {
                **metadata,
                "wrapped_data_key": _b64encode(wrapped.ciphertext),
                "nonce": _b64encode(nonce),
                "ciphertext": _b64encode(encrypted),
            }
            encoded = json.dumps(
                document, separators=(",", ":"), sort_keys=True, ensure_ascii=True
            ).encode("utf-8")
            if len(encoded) > MAX_ENVELOPE_BYTES:
                raise EnvelopeFormatError("credential envelope exceeds its size limit")
            return ENVELOPE_PREFIX + _b64encode(encoded)
        finally:
            BoundedTTLKeyCache._wipe(data_key)

    def encrypt_text(
        self, plaintext: str, *, context: Mapping[str, str] | None = None
    ) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("credential plaintext must be text")
        return self.encrypt_bytes(plaintext.encode("utf-8"), context=context)

    def _parse(self, ciphertext: str) -> tuple[dict[str, object], WrappedDataKey, bytes, bytes]:
        if not self.is_envelope(ciphertext):
            raise EnvelopeFormatError("credential does not use a supported envelope")
        encoded = ciphertext[len(ENVELOPE_PREFIX):]
        document_bytes = _b64decode(encoded, maximum=MAX_ENVELOPE_BYTES)
        try:
            document = json.loads(document_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EnvelopeFormatError("credential envelope document is invalid") from None
        if not isinstance(document, dict):
            raise EnvelopeFormatError("credential envelope document is invalid")
        expected_fields = {
            "schema", "version", "key_provider", "key_id", "key_version",
            "wrap_algorithm", "data_algorithm", "context_digest",
            "wrapped_data_key", "nonce", "ciphertext",
        }
        if set(document) != expected_fields:
            raise EnvelopeFormatError("credential envelope fields are invalid")
        if document.get("schema") != ENVELOPE_SCHEMA or document.get("version") != 1:
            raise EnvelopeFormatError("credential envelope version is unsupported")
        if document.get("data_algorithm") != DATA_ALGORITHM:
            raise EnvelopeFormatError("credential envelope algorithm is unsupported")
        provider = _bounded_metadata(document.get("key_provider"), "key provider", 128)
        key_id = _bounded_metadata(document.get("key_id"), "key id")
        key_version = _bounded_metadata(document.get("key_version"), "key version", 128)
        algorithm = _bounded_metadata(
            document.get("wrap_algorithm"), "wrap algorithm", 128
        )
        digest = document.get("context_digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise EnvelopeFormatError("credential envelope context is invalid")
        try:
            bytes.fromhex(digest)
        except ValueError:
            raise EnvelopeFormatError("credential envelope context is invalid") from None
        wrapped_bytes = _b64decode(
            document.get("wrapped_data_key"), maximum=MAX_WRAPPED_KEY_BYTES
        )
        nonce = _b64decode(document.get("nonce"), maximum=12)
        if len(nonce) != 12:
            raise EnvelopeFormatError("credential envelope nonce is invalid")
        encrypted = _b64decode(document.get("ciphertext"), maximum=MAX_ENVELOPE_BYTES)
        wrapped = WrappedDataKey(provider, key_id, key_version, algorithm, wrapped_bytes)
        metadata = {key: document[key] for key in expected_fields if key not in {
            "wrapped_data_key", "nonce", "ciphertext"
        }}
        return metadata, wrapped, nonce, encrypted

    @staticmethod
    def _cache_key(wrapped: WrappedDataKey, context_digest: str) -> str:
        material = b"\x00".join((
            wrapped.provider.encode(), wrapped.key_id.encode(), wrapped.key_version.encode(),
            wrapped.algorithm.encode(), context_digest.encode(), wrapped.ciphertext,
        ))
        return hashlib.sha256(material).hexdigest()

    def decrypt_with_metadata(
        self, ciphertext: str, *, context: Mapping[str, str] | None = None
    ) -> DecryptionResult:
        if not isinstance(ciphertext, str) or not ciphertext:
            raise EnvelopeFormatError("credential ciphertext is invalid")
        normalized_context = _normalize_context(context)
        if not self.is_envelope(ciphertext):
            if self.legacy_decryptor is None:
                raise EnvelopeFormatError("legacy credential decryption is not enabled")
            plaintext = self.legacy_decryptor.decrypt(ciphertext)
            return DecryptionResult(plaintext, "legacy-fernet", "", "", True)

        metadata, wrapped, nonce, encrypted = self._parse(ciphertext)
        actual_digest = _context_digest(normalized_context)
        expected_digest = str(metadata["context_digest"])
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise EnvelopeDecryptionError("credential encryption context does not match")
        if wrapped.provider != self.kms.provider:
            raise EnvelopeDecryptionError("credential KMS provider is unavailable")

        cache_key = self._cache_key(wrapped, actual_digest)
        data_key = self.cache.get(cache_key)
        if data_key is None:
            try:
                data_key = self.kms.unwrap_data_key(
                    wrapped, encryption_context=normalized_context
                )
            except KMSUnavailable:
                raise
            except Exception:
                raise KMSUnavailable("managed KMS unwrap operation unavailable") from None
            if not isinstance(data_key, bytes) or len(data_key) != 32:
                raise EnvelopeDecryptionError("managed KMS returned an invalid data key")
            self.cache.put(cache_key, data_key)
        try:
            plaintext = AESGCM(data_key).decrypt(nonce, encrypted, _aad(metadata))
        except (InvalidTag, ValueError):
            raise EnvelopeDecryptionError("credential envelope authentication failed") from None
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise EnvelopeDecryptionError("credential plaintext exceeds its size limit")

        needs_rotation = (
            wrapped.key_id != self.key_id
            or wrapped.key_version != self.key_version
            or (
                self.wrap_algorithm != "provider-default"
                and wrapped.algorithm != self.wrap_algorithm
            )
        )
        return DecryptionResult(
            plaintext=plaintext,
            source="envelope-v1",
            key_id=wrapped.key_id,
            key_version=wrapped.key_version,
            needs_rotation=needs_rotation,
        )

    def decrypt_bytes(
        self, ciphertext: str, *, context: Mapping[str, str] | None = None
    ) -> bytes:
        return self.decrypt_with_metadata(ciphertext, context=context).plaintext

    def decrypt_text(
        self, ciphertext: str, *, context: Mapping[str, str] | None = None
    ) -> str:
        try:
            return self.decrypt_bytes(ciphertext, context=context).decode("utf-8")
        except UnicodeDecodeError:
            raise EnvelopeDecryptionError("credential plaintext encoding is invalid") from None

    def reencrypt(
        self, ciphertext: str, *, context: Mapping[str, str] | None = None
    ) -> str:
        result = self.decrypt_with_metadata(ciphertext, context=context)
        return self.encrypt_bytes(result.plaintext, context=context)

    def inspect_metadata(self, ciphertext: str) -> dict[str, object]:
        """Return non-secret envelope metadata without calling KMS."""
        metadata, _, _, _ = self._parse(ciphertext)
        return dict(metadata)


def build_envelope_cipher(
    *,
    adapter: ManagedKMS | None = None,
    legacy_keys: Iterable[str | bytes] = (),
    environ: Mapping[str, str] | None = None,
) -> EnvelopeCipher:
    kms, settings = kms_from_environment(adapter=adapter, environ=environ)
    legacy_values = tuple(legacy_keys)
    legacy = LegacyFernetDecryptor(legacy_values) if legacy_values else None
    return EnvelopeCipher(
        kms,
        key_id=settings.key_id,
        key_version=settings.key_version,
        wrap_algorithm=settings.algorithm,
        legacy_decryptor=legacy,
        cache=BoundedTTLKeyCache(
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
        ),
    )


def rotate_envelopes(
    records: Iterable[RotationRecord],
    *,
    cipher: EnvelopeCipher,
    persist: Callable[[int, str], None] | None = None,
    dry_run: bool = True,
    max_records: int = 1000,
) -> RotationSummary:
    """Inspect or re-encrypt a bounded batch; record identities stay with the caller.

    ``persist`` receives the zero-based batch position, not a tenant or database ID,
    preventing identifiers from entering errors or logs.  Production callers should
    make their callback an optimistic, transactional compare-and-swap.
    """

    if not 1 <= max_records <= 10_000:
        raise ValueError("rotation batch limit is outside safe bounds")
    if not dry_run and persist is None:
        raise ValueError("rotation persistence callback is required")
    inspected = reencrypted = already_current = failed = 0
    for position, record in enumerate(records):
        if position >= max_records:
            break
        inspected += 1
        try:
            result = cipher.decrypt_with_metadata(
                record.ciphertext, context=record.context
            )
            if not result.needs_rotation:
                already_current += 1
                continue
            reencrypted += 1
            if not dry_run:
                replacement = cipher.encrypt_bytes(result.plaintext, context=record.context)
                assert persist is not None
                persist(position, replacement)
        except (EnvelopeError, KMSUnavailable):
            failed += 1
    return RotationSummary(inspected, reencrypted, already_current, failed, dry_run)


__all__ = [
    "BoundedTTLKeyCache",
    "DATA_ALGORITHM",
    "DecryptionResult",
    "ENVELOPE_PREFIX",
    "EnvelopeCipher",
    "EnvelopeDecryptionError",
    "EnvelopeError",
    "EnvelopeFormatError",
    "LegacyFernetDecryptor",
    "RotationRecord",
    "RotationSummary",
    "build_envelope_cipher",
    "rotate_envelopes",
]
