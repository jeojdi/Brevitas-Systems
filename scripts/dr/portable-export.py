#!/usr/bin/env python3
"""Stream SQL export records through an exact-context decryption boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

MAX_LINE_BYTES = 20 * 1024 * 1024


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def has_ciphertext_key(value: object) -> bool:
    if isinstance(value, dict):
        return any("ciphertext" in str(key).lower() or has_ciphertext_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(has_ciphertext_key(item) for item in value)
    return False


def validate_context(kind: str, context: object) -> dict[str, str]:
    if not isinstance(context, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in context.items()
    ):
        fail("encrypted export context is invalid")
    keys = set(context)
    if kind == "application_envelope" and context.get("purpose") == "provider_credential":
        expected = {"purpose", "key_hash"}
    elif kind == "application_envelope" and context.get("purpose") == "durable_job":
        expected = {"purpose", "job_id", "organization_id", "field"}
        if context.get("field") not in {"payload", "result"}:
            fail("durable-job export field is invalid")
    elif kind == "semantic_cache" and context.get("purpose") == "semantic-response-cache":
        expected = {"purpose", "tenant_namespace", "exact_hash", "model_identity"}
    else:
        fail("encrypted export kind/purpose is unsupported")
    if keys != expected:
        fail("encrypted export context fields are not exact")
    return context


def decrypt(command: str, data: dict) -> object:
    kind = data.get("encryption_kind")
    ciphertext = data.get("ciphertext")
    context = validate_context(kind, data.get("context"))
    if not isinstance(ciphertext, str) or not ciphertext:
        fail("encrypted export row has no ciphertext")
    context_hash = hashlib.sha256(canonical(context)).hexdigest()
    ciphertext_hash = hashlib.sha256(ciphertext.encode("utf-8")).hexdigest()
    request = {
        "schema": "brevitas.compliance-decrypt-request.v1",
        "operation": "decrypt",
        "encryption_kind": kind,
        "ciphertext": ciphertext,
        "ciphertext_sha256": ciphertext_hash,
        "context": context,
        "context_sha256": context_hash,
    }
    try:
        result = subprocess.run(
            [command], input=canonical(request) + b"\n", capture_output=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("managed decryption command is unavailable")
    if result.returncode != 0 or len(result.stdout) > MAX_LINE_BYTES:
        fail("managed decryption command failed")
    try:
        response = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("managed decryption response is invalid")
    if not isinstance(response, dict) \
            or response.get("schema") != "brevitas.compliance-decrypt-response.v1" \
            or response.get("status") != "decrypted" \
            or response.get("context_sha256") != context_hash \
            or response.get("ciphertext_sha256") != ciphertext_hash \
            or "plaintext" not in response:
        fail("managed decryption response binding failed")
    return response["plaintext"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decrypt-command-env", default="BREVITAS_COMPLIANCE_DECRYPT_COMMAND")
    args = parser.parse_args()
    if not args.decrypt_command_env.isidentifier() or not args.decrypt_command_env.isupper():
        fail("decryption command environment variable name is invalid")
    command = os.environ.get(args.decrypt_command_env, "")
    if command:
        path = Path(command)
        if not path.is_absolute() or path.is_symlink() or not path.is_file() \
                or not os.access(path, os.X_OK):
            fail("managed decryption command must be an absolute executable non-symlink file")

    digest = hashlib.sha256()
    record_count = 0
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_LINE_BYTES:
            fail("export record exceeds its safety bound")
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            fail("database export emitted invalid JSON")
        if not isinstance(record, dict) or set(record) != {"record_type", "data"}:
            fail("database export record shape is invalid")
        if record["record_type"] == "encrypted_content":
            if not command:
                fail("managed decryption command is required for encrypted export content")
            data = record.get("data")
            if not isinstance(data, dict) or set(data) != {
                "encryption_kind", "ciphertext", "context", "output_record_type",
                "content_field", "metadata",
            }:
                fail("encrypted export envelope shape is invalid")
            if not isinstance(data["metadata"], dict) \
                    or not isinstance(data["output_record_type"], str) \
                    or not isinstance(data["content_field"], str):
                fail("encrypted export metadata is invalid")
            plaintext = decrypt(command, data)
            output_data = dict(data["metadata"])
            output_data[data["content_field"]] = plaintext
            record = {"record_type": data["output_record_type"], "data": output_data}
        if has_ciphertext_key(record):
            fail("portable export retained ciphertext-only content")
        encoded = canonical(record) + b"\n"
        sys.stdout.buffer.write(encoded)
        digest.update(encoded)
        record_count += 1

    integrity = {
        "record_type": "export_integrity",
        "data": {
            "schema": "brevitas.portable-export-integrity.v1",
            "record_count": record_count,
            "records_sha256": digest.hexdigest(),
            "ciphertext_only_records": 0,
        },
    }
    sys.stdout.buffer.write(canonical(integrity) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
