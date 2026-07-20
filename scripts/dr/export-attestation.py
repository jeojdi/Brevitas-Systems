#!/usr/bin/env python3
"""Create or verify a request-bound HMAC export attestation sidecar."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

EXPECTED_KEYS = {
    "schema", "status", "request_id", "tenant_id", "request_scope", "subject_id",
    "actor_id", "target_id", "environment", "requested_at", "due_at",
    "deadline_breached", "artifact_file", "artifact_sha256", "artifact_retention_hours",
    "signed_at", "artifact_expires_at", "portable_record_count", "portable_records_sha256",
    "ciphertext_only_records", "general_telemetry_content_exported", "signature_algorithm",
    "signature",
}


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ERROR: {message}")


def canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() \
            or before.st_mode & 0o077 or before.st_nlink != 1:
        fail("export artifact file descriptor is not owner-only and stable")
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        fail("export artifact changed while hashing")
    return digest.hexdigest()


def open_artifact(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError:
        fail("attestation artifact must be an owner-only regular non-symlink file")


def write_new(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        fail("export attestation sidecar could not be created exclusively")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        fail("attestation timestamp is invalid")
    return parsed


def unsigned(document: dict) -> dict:
    return {key: value for key, value in document.items() if key != "signature"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["create", "verify"])
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--artifact-fd", type=int, default=-1)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--scope", required=True, choices=["tenant", "member", "customer"])
    parser.add_argument("--subject-id", default="")
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--requested-at", required=True)
    parser.add_argument("--due-at", required=True)
    parser.add_argument("--deadline-breached", required=True, choices=["true", "false"])
    parser.add_argument("--hmac-key-env", default="BREVITAS_EXPORT_EVIDENCE_HMAC_KEY")
    args = parser.parse_args()
    if not args.hmac_key_env.isidentifier() or not args.hmac_key_env.isupper():
        fail("HMAC key environment variable name is invalid")
    key = os.environ.get(args.hmac_key_env, "").encode("utf-8")
    if len(key) < 32:
        fail("export evidence HMAC key must contain at least 32 bytes")
    if not args.summary.is_file() or args.summary.is_symlink():
        fail("attestation summary must be a regular non-symlink file")
    owned_descriptor = -1
    descriptor = args.artifact_fd
    if descriptor < 0:
        descriptor = owned_descriptor = open_artifact(args.artifact)
    try:
        artifact_sha256 = sha256_fd(descriptor)
    finally:
        if owned_descriptor >= 0:
            os.close(owned_descriptor)
    if args.scope == "tenant" and args.subject_id \
            or args.scope != "tenant" and not args.subject_id:
        fail("attestation subject binding is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        fail("artifact digest is invalid")
    requested = parse_time(args.requested_at)
    due = parse_time(args.due_at)
    if due <= requested or due > requested + timedelta(days=30):
        fail("attestation deadline binding is invalid")
    try:
        summary = json.loads(args.summary.read_text())
    except (OSError, json.JSONDecodeError):
        fail("portable export summary is invalid")
    if set(summary) != {"schema", "record_count", "records_sha256", "ciphertext_only_records"} \
            or summary.get("schema") != "brevitas.portable-export-verification.v1" \
            or not isinstance(summary.get("record_count"), int) \
            or summary.get("record_count") < 0 \
            or not re.fullmatch(r"[0-9a-f]{64}", str(summary.get("records_sha256", ""))) \
            or summary.get("ciphertext_only_records") != 0:
        fail("portable export summary contract mismatch")

    if args.mode == "create":
        if args.sidecar.exists() or args.sidecar.is_symlink():
            fail("export attestation sidecar already exists")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        document = {
            "schema": "brevitas.export-attestation.v1",
            "status": "portable_export_verified",
            "request_id": args.request_id,
            "tenant_id": args.tenant_id,
            "request_scope": args.scope,
            "subject_id": args.subject_id or None,
            "actor_id": args.actor_id,
            "target_id": args.target_id,
            "environment": args.environment,
            "requested_at": args.requested_at,
            "due_at": args.due_at,
            "deadline_breached": args.deadline_breached == "true",
            "artifact_file": args.artifact.name,
            "artifact_sha256": artifact_sha256,
            "artifact_retention_hours": 24,
            "signed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "artifact_expires_at": (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "portable_record_count": summary["record_count"],
            "portable_records_sha256": summary["records_sha256"],
            "ciphertext_only_records": 0,
            "general_telemetry_content_exported": False,
            "signature_algorithm": "HMAC-SHA256",
        }
        document["signature"] = hmac.new(key, canonical(document), hashlib.sha256).hexdigest()
        write_new(args.sidecar, json.dumps(document, indent=2, sort_keys=True) + "\n")
        return 0

    if not args.sidecar.is_file() or args.sidecar.is_symlink():
        fail("existing export requires a regular signed attestation sidecar")
    try:
        document = json.loads(args.sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        fail("export attestation sidecar is invalid")
    if not isinstance(document, dict) or set(document) != EXPECTED_KEYS:
        fail("export attestation fields are not exact")
    signature = document.get("signature")
    expected_signature = hmac.new(key, canonical(unsigned(document)), hashlib.sha256).hexdigest()
    expected = {
        "schema": "brevitas.export-attestation.v1",
        "status": "portable_export_verified",
        "request_id": args.request_id,
        "tenant_id": args.tenant_id,
        "request_scope": args.scope,
        "subject_id": args.subject_id or None,
        "actor_id": args.actor_id,
        "target_id": args.target_id,
        "environment": args.environment,
        "requested_at": args.requested_at,
        "due_at": args.due_at,
        "deadline_breached": args.deadline_breached == "true",
        "artifact_file": args.artifact.name,
        "artifact_sha256": artifact_sha256,
        "artifact_retention_hours": 24,
        "portable_record_count": summary["record_count"],
        "portable_records_sha256": summary["records_sha256"],
        "ciphertext_only_records": 0,
        "general_telemetry_content_exported": False,
        "signature_algorithm": "HMAC-SHA256",
    }
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected_signature) \
            or any(document.get(key) != value for key, value in expected.items()):
        fail("export attestation signature or request binding failed")
    signed = parse_time(document["signed_at"])
    expires = parse_time(document["artifact_expires_at"])
    now = datetime.now(timezone.utc)
    if expires != signed + timedelta(hours=24) or now >= expires \
            or signed > now + timedelta(minutes=5):
        fail("export attestation is stale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
