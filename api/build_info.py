"""Validated, non-secret build identity for deployment attestation."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone


_FULL_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RELEASE_VERSION = re.compile(
    r"^(?=.{1,64}$)v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RFC3339_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$")
_COMMIT_VARIABLES = (
    "BREVITAS_BUILD_SHA",
    "RAILWAY_GIT_COMMIT_SHA",
    "VERCEL_GIT_COMMIT_SHA",
    "GITHUB_SHA",
)


def _values(environ: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    return [str(environ.get(name) or "").strip() for name in names
            if str(environ.get(name) or "").strip()]


def _commit_sha(environ: Mapping[str, str], *, required: bool) -> str:
    supplied = [value.lower() for value in _values(environ, _COMMIT_VARIABLES)]
    if any(not _FULL_COMMIT_SHA.fullmatch(value) for value in supplied):
        raise RuntimeError("Build commit identity must be a full immutable Git SHA")
    distinct = set(supplied)
    if len(distinct) > 1:
        raise RuntimeError("Conflicting build commit identities were supplied")
    if not distinct:
        if required:
            raise RuntimeError("Production requires a full immutable build commit SHA")
        return ""
    return distinct.pop()


def _build_timestamp(environ: Mapping[str, str]) -> str:
    raw = str(environ.get("BREVITAS_BUILD_TIMESTAMP") or "").strip()
    if not raw:
        return ""
    if not _RFC3339_TIMESTAMP.fullmatch(raw):
        raise RuntimeError("Build timestamp must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise RuntimeError("Build timestamp must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("Build timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_identity(
    *,
    required: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return only validated immutable identity fields that are safe to publish."""
    source = os.environ if environ is None else environ
    identity: dict[str, str] = {}
    commit_sha = _commit_sha(source, required=required)
    if commit_sha:
        identity["commit_sha"] = commit_sha

    timestamp = _build_timestamp(source)
    if timestamp:
        identity["built_at"] = timestamp

    version = str(source.get("BREVITAS_BUILD_VERSION") or "").strip()
    if version:
        if not _RELEASE_VERSION.fullmatch(version):
            raise RuntimeError("Build version is invalid")
        identity["version"] = version

    image_digest = str(source.get("BREVITAS_IMAGE_DIGEST") or "").strip().lower()
    if image_digest:
        if not _IMAGE_DIGEST.fullmatch(image_digest):
            raise RuntimeError("Build image digest must be an immutable sha256 digest")
        identity["image_digest"] = image_digest
    return identity


def validate_production_build_identity(production: bool) -> None:
    """Fail startup before accepting work when production provenance is absent or ambiguous."""
    build_identity(required=production)
