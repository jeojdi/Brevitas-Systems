"""Fail-closed construction of the API credential envelope cipher."""
from __future__ import annotations

from typing import Iterable, Mapping

from brevitas.security import EnvelopeCipher, ManagedKMS, build_envelope_cipher


def credential_cipher_from_environment(
    *,
    adapter: ManagedKMS | None = None,
    legacy_keys: Iterable[str | bytes] = (),
    environ: Mapping[str, str] | None = None,
) -> EnvelopeCipher:
    """Build a cipher only from explicit KMS and optional migration inputs.

    No secret file is read or generated.  Production always requires a real
    ``ManagedKMS`` adapter; legacy Fernet keys are accepted only as explicit
    decrypt-only inputs during a controlled re-encryption window.
    """

    return build_envelope_cipher(
        adapter=adapter,
        legacy_keys=legacy_keys,
        environ=environ,
    )


__all__ = ["credential_cipher_from_environment"]
