#!/usr/bin/env python3
"""Verify a decrypted portable export without logging or persisting its content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

MAX_LINE_BYTES = 20 * 1024 * 1024


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def has_ciphertext_key(value: object) -> bool:
    if isinstance(value, dict):
        return any("ciphertext" in str(key).lower() or has_ciphertext_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(has_ciphertext_key(item) for item in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-file", required=True, type=Path)
    args = parser.parse_args()
    if args.summary_file.exists() or args.summary_file.is_symlink():
        fail("portable export verification summary already exists")
    digest = hashlib.sha256()
    record_count = 0
    integrity = None
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_LINE_BYTES:
            fail("portable export record exceeds its safety bound")
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            fail("portable export contains invalid JSON")
        if not isinstance(record, dict) or set(record) != {"record_type", "data"}:
            fail("portable export record shape is invalid")
        if integrity is not None:
            fail("portable export integrity record is not final")
        if record["record_type"] == "export_integrity":
            integrity = record["data"]
            continue
        if record["record_type"] == "encrypted_content" or has_ciphertext_key(record):
            fail("portable export contains undecrypted ciphertext")
        digest.update(raw)
        record_count += 1
    if not isinstance(integrity, dict) \
            or integrity.get("schema") != "brevitas.portable-export-integrity.v1" \
            or integrity.get("record_count") != record_count \
            or integrity.get("records_sha256") != digest.hexdigest() \
            or integrity.get("ciphertext_only_records") != 0:
        fail("portable export integrity proof failed")
    content = json.dumps({
        "schema": "brevitas.portable-export-verification.v1",
        "record_count": record_count,
        "records_sha256": digest.hexdigest(),
        "ciphertext_only_records": 0,
    }, separators=(",", ":"), sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(args.summary_file, flags, 0o600)
    except OSError:
        fail("portable export verification summary could not be created exclusively")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
