#!/usr/bin/env python3
"""Age-verify and HMAC-attest one exact, stably opened export artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def hash_fd(descriptor: int) -> str:
    value = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return value.hexdigest()
        value.update(chunk)
        offset += len(chunk)


def hash_path(path: Path) -> str:
    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("attestation sidecar is unavailable")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() \
                or info.st_mode & 0o077 or info.st_nlink != 1:
            fail("attestation sidecar is not an owner-only stable file")
        return hash_fd(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--identity-file", required=True, type=Path)
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
    parser.add_argument("--hmac-key-env", required=True)
    args = parser.parse_args()

    flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        descriptor = os.open(args.artifact, flags)
    except OSError:
        fail("portable export must be a regular non-symlink file")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() \
                or opened.st_mode & 0o077 or opened.st_nlink != 1:
            fail("portable export must be owner-only and unshared")
        before_hash = hash_fd(descriptor)
        before_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)

        age = subprocess.Popen(
            ["age", "--decrypt", "--identity", str(args.identity_file)],
            stdin=descriptor, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert age.stdout is not None
        verifier = subprocess.run(
            [str(Path(__file__).with_name("verify-portable-export.py")),
             "--summary-file", str(args.summary)],
            stdin=age.stdout, capture_output=True, check=False,
        )
        age.stdout.close()
        age_stderr = age.stderr.read(4097) if age.stderr is not None else b""
        age_status = age.wait(timeout=60)
        if age_status != 0 or len(age_stderr) > 4096:
            fail("age decryption verification failed")
        if verifier.returncode != 0:
            message = verifier.stderr.decode("utf-8", "replace")[:1024].strip()
            fail(message or "portable export verification failed")

        common = [
            "--sidecar", str(args.sidecar), "--artifact", str(args.artifact),
            "--artifact-fd", str(descriptor), "--summary", str(args.summary),
            "--request-id", args.request_id, "--tenant-id", args.tenant_id,
            "--scope", args.scope, "--subject-id", args.subject_id,
            "--actor-id", args.actor_id, "--target-id", args.target_id,
            "--environment", args.environment, "--requested-at", args.requested_at,
            "--due-at", args.due_at, "--deadline-breached", args.deadline_breached,
            "--hmac-key-env", args.hmac_key_env,
        ]
        attestation_command = str(Path(__file__).with_name("export-attestation.py"))
        if not args.sidecar.exists():
            created = subprocess.run(
                [attestation_command, "create", *common], pass_fds=(descriptor,),
                capture_output=True, check=False, timeout=30,
            )
            if created.returncode != 0:
                fail(created.stderr.decode("utf-8", "replace")[:1024].strip()
                     or "export attestation creation failed")
        verified = subprocess.run(
            [attestation_command, "verify", *common], pass_fds=(descriptor,),
            capture_output=True, check=False, timeout=30,
        )
        if verified.returncode != 0:
            fail(verified.stderr.decode("utf-8", "replace")[:1024].strip()
                 or "export attestation verification failed")

        after = os.fstat(descriptor)
        if before_identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) \
                or hash_fd(descriptor) != before_hash:
            fail("portable export changed during verification and attestation")
        try:
            path_state = os.stat(args.artifact, follow_symlinks=False)
        except OSError:
            fail("portable export path changed during verification")
        if (path_state.st_dev, path_state.st_ino) != (after.st_dev, after.st_ino) \
                or not stat.S_ISREG(path_state.st_mode):
            fail("portable export path was concurrently substituted")
    finally:
        os.close(descriptor)

    try:
        summary = json.loads(args.summary.read_text())
        sidecar = json.loads(args.sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        fail("portable export proof could not be read")
    if sidecar.get("artifact_sha256") != before_hash:
        fail("portable export sidecar does not bind the opened artifact")
    print(json.dumps({
        "artifact_sha256": before_hash,
        "attestation_sha256": hash_path(args.sidecar),
        "portable_record_count": summary["record_count"],
        "portable_records_sha256": summary["records_sha256"],
    }, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
