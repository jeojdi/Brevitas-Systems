#!/usr/bin/env python3
"""Guarded retention for encrypted logical backups and their sidecars."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys

RETENTION_DAYS = 35
BACKUP_RE = re.compile(r"^brevitas-(?P<source>[A-Za-z0-9._:-]{3,128})-(?P<stamp>\d{8}T\d{6}Z)\.dump\.age$")


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["local", "test", "development", "staging", "production"])
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="delete expired backup sets; default is dry-run")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{3,128}", args.source_id) or "@" in args.source_id:
        fail("source ID must be an opaque 3-128 character identifier")
    if args.environment == "production" and not args.allow_production:
        fail("production is refused by default; pass --allow-production under an approved change")
    directory = args.backup_dir
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() in {Path("/"), Path.home().resolve()}:
        fail("backup directory must be an existing safe non-symlink directory")
    if args.apply and args.confirm != f"PRUNE:{args.source_id}:35D":
        fail("confirmation mismatch; use the exact documented operation token")

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    expired: list[tuple[Path, Path, Path]] = []
    prefix = f"brevitas-{args.source_id}-"
    for backup in sorted(directory.iterdir()):
        if not backup.name.startswith(prefix) or not backup.name.endswith(".dump.age"):
            continue
        match = BACKUP_RE.fullmatch(backup.name)
        if not match or match.group("source") != args.source_id:
            fail("backup filename does not match the explicit source ID")
        if backup.is_symlink() or not backup.is_file():
            fail("backup candidate must be a regular non-symlink file")
        stem = backup.name.removesuffix(".dump.age")
        manifest_path = directory / f"{stem}.manifest.json"
        evidence_path = directory / f"{stem}.backup-evidence.json"
        if any(path.is_symlink() or not path.is_file() for path in (manifest_path, evidence_path)):
            fail(f"backup set is missing a regular immutable sidecar: {backup.name}")
        if manifest_path.stat().st_size > 10 * 1024 * 1024 or evidence_path.stat().st_size > 1024 * 1024:
            fail(f"backup sidecar exceeds its safety bound: {backup.name}")
        try:
            manifest_raw = manifest_path.read_bytes()
            manifest = json.loads(manifest_raw)
            evidence = json.loads(evidence_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            fail(f"backup set has an invalid manifest/evidence document: {backup.name}")
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        ciphertext_sha256 = sha256_file(backup)
        tables = manifest.get("tables")
        if (
            manifest.get("schema") != "brevitas.logical-backup-manifest.v2"
            or manifest.get("target_contract") != "brevitas-ephemeral-postgres-v1"
            or manifest.get("postgresql_major") != 16
            or manifest.get("required_extensions") != ["pgcrypto", "vector"]
            or manifest.get("required_roles") != ["anon", "authenticated", "service_role"]
            or manifest.get("backup_source_id") != args.source_id
            or manifest.get("source_environment") != args.environment
            or manifest.get("ciphertext_file") != backup.name
            or manifest.get("ciphertext_sha256") != ciphertext_sha256
            or manifest.get("ciphertext_bytes") != backup.stat().st_size
            or manifest.get("retention_days") != RETENTION_DAYS
            or not isinstance(tables, list)
            or not tables
        ):
            fail(f"backup manifest validation failed: {backup.name}")
        if (
            evidence.get("schema") != "brevitas.backup-evidence.v2"
            or evidence.get("result") != "completed"
            or evidence.get("backup_source_id") != args.source_id
            or evidence.get("source_environment") != args.environment
            or evidence.get("manifest_file") != manifest_path.name
            or evidence.get("manifest_sha256") != manifest_sha256
        ):
            fail(f"immutable backup evidence validation failed: {backup.name}")
        try:
            created_at = dt.datetime.strptime(
                manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
        except (KeyError, TypeError, ValueError):
            fail(f"backup manifest timestamp is invalid: {backup.name}")
        if created_at > now + dt.timedelta(minutes=5):
            fail(f"backup manifest timestamp is in the future: {backup.name}")
        if created_at <= cutoff:
            expired.append((backup, manifest_path, evidence_path))

    for backup, manifest_path, evidence_path in expired:
        sidecars = [backup, manifest_path, evidence_path]
        if args.apply:
            for path in sidecars:
                if path.is_symlink() or not path.is_file():
                    fail("backup set changed during retention processing")
            for path in sidecars:
                path.unlink()
        print(f"{'DELETE' if args.apply else 'WOULD_DELETE'} {backup.name}")
    print(f"result={'applied' if args.apply else 'dry-run'} retention_days={RETENTION_DAYS} expired_sets={len(expired)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
