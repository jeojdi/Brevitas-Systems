import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DR = ROOT / "scripts" / "dr"


def read(path: str) -> str:
    return (ROOT / path).read_text()


def run(*arguments: str, env: dict[str, str] | None = None,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    process_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    if env:
        process_env.update(env)
    return subprocess.run(
        list(arguments), cwd=ROOT, env=process_env, input=input_text,
        capture_output=True, text=True, check=False
    )


def write_backup_set(directory: Path, *, source: str, environment: str,
                     created_at: dt.datetime, suffix: str = "") -> tuple[Path, Path, Path, str]:
    stamp = created_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if suffix:
        stamp = suffix
    base = f"brevitas-{source}-{stamp}"
    backup = directory / f"{base}.dump.age"
    manifest_path = directory / f"{base}.manifest.json"
    evidence_path = directory / f"{base}.backup-evidence.json"
    backup.write_bytes(f"encrypted-{stamp}".encode())
    manifest = {
        "schema": "brevitas.logical-backup-manifest.v2",
        "target_contract": "brevitas-ephemeral-postgres-v1",
        "postgresql_major": 16,
        "required_extensions": ["pgcrypto", "vector"],
        "required_roles": ["anon", "authenticated", "service_role"],
        "backup_source_id": source,
        "source_environment": environment,
        "created_at": created_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ciphertext_file": backup.name,
        "ciphertext_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "ciphertext_bytes": backup.stat().st_size,
        "encryption": "age-v1",
        "retention_days": 35,
        "tables": [{"schema": "public", "table": "organizations", "rows": 1}],
    }
    manifest_raw = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(manifest_raw)
    evidence = {
        "schema": "brevitas.backup-evidence.v2",
        "operation": "logical-backup",
        "result": "completed",
        "backup_source_id": source,
        "source_environment": environment,
        "completed_at": manifest["created_at"],
        "manifest_file": manifest_path.name,
        "manifest_sha256": hashlib.sha256(manifest_raw.encode()).hexdigest(),
        "evidence_contains_customer_content": False,
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return backup, manifest_path, evidence_path, evidence["manifest_sha256"]


def write_deletion_artifact(
    directory: Path, *, source: str, environment: str, manifest_sha256: str,
    issued_at: dt.datetime, evidence_reference: str = "evidence:deletions:immutable:001",
    tombstones: list[dict[str, object]] | None = None,
    backup_created_at: dt.datetime | None = None,
) -> tuple[Path, str]:
    artifact = directory / "deletions.json"
    document = {
        "schema": "brevitas.deletion-artifact.v1",
        "backup_source_id": source,
        "source_environment": environment,
        "backup_manifest_sha256": manifest_sha256,
        "backup_created_at": (backup_created_at or issued_at - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_reference": evidence_reference,
        "tombstones": tombstones or [],
    }
    artifact.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return artifact, hashlib.sha256(artifact.read_bytes()).hexdigest()


def write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o700)


def write_fake_compliance_psql(directory: Path) -> None:
    write_executable(directory / "psql", """#!/usr/bin/env python3
import os
import sys

arguments = sys.argv[1:]
if any(value.startswith("--set=") for value in arguments) \
        and any(value in {"-c", "--command"} or value.startswith("--command=")
                for value in arguments):
    print("parameterized SQL passed through non-interpolating psql command mode", file=sys.stderr)
    raise SystemExit(97)
sql = " ".join(arguments) + " " + sys.stdin.read()
status = os.environ.get("FAKE_REQUEST_STATUS", "processing")
digest = os.environ.get("FAKE_ARTIFACT_SHA", "")
attestation = os.environ.get("FAKE_ATTESTATION_SHA", "")
record_count = os.environ.get("FAKE_RECORD_COUNT", "1")
records_hash = os.environ.get("FAKE_RECORDS_SHA", "")
if "to_regclass('public.data_subject_requests')" in sql:
    print("t|t|t|t|t|t|t|t")
elif "select status, (deadline_breached or" in sql:
    stored = digest if status == "completed" else ""
    print(f"{status}|f|t|t|{stored}|tenant||2026-01-01T00:00:00Z|2026-01-31T00:00:00Z")
elif "compliance_complete_export" in sql:
    print("completed")
elif "select status,deadline_breached::text" in sql:
    print(f"completed|false|{digest}|{attestation}|{record_count}|{records_hash}|2026-01-02T00:00:00Z")
else:
    print("unexpected fake psql query", file=sys.stderr)
    sys.exit(9)
""")


def write_fake_age(directory: Path) -> None:
    write_executable(directory / "age", """#!/usr/bin/env python3
import pathlib
import shutil
import sys

args=sys.argv[1:]
if "--decrypt" in args:
    inputs=[value for index,value in enumerate(args)
            if not value.startswith("-") and (index==0 or args[index-1]!="--identity")]
    if inputs:
        with pathlib.Path(inputs[-1]).open("rb") as source:
            shutil.copyfileobj(source,sys.stdout.buffer)
    else:
        shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)
    raise SystemExit(0)
if "--encrypt" in args and "--output" in args:
    output=pathlib.Path(args[args.index("--output")+1])
    with output.open("wb") as destination:
        shutil.copyfileobj(sys.stdin.buffer,destination)
    raise SystemExit(0)
raise SystemExit(2)
""")


def write_portable_artifact(path: Path, records: list[dict[str, object]]) -> dict[str, object]:
    digest = hashlib.sha256()
    encoded_records = []
    for record in records:
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        encoded_records.append(encoded)
        digest.update(encoded.encode())
    integrity = {
        "record_type": "export_integrity",
        "data": {
            "schema": "brevitas.portable-export-integrity.v1",
            "record_count": len(records),
            "records_sha256": digest.hexdigest(),
            "ciphertext_only_records": 0,
        },
    }
    path.write_text("".join(encoded_records) + json.dumps(
        integrity, separators=(",", ":"), sort_keys=True
    ) + "\n")
    path.chmod(0o600)
    return {
        "schema": "brevitas.portable-export-verification.v1",
        "record_count": len(records),
        "records_sha256": digest.hexdigest(),
        "ciphertext_only_records": 0,
    }


def create_export_attestation(
    directory: Path, artifact: Path, summary: dict[str, object], *,
    request_id: str, tenant_id: str, key: str,
) -> tuple[Path, dict[str, object]]:
    summary_path = directory / "portable-summary.json"
    summary_path.write_text(json.dumps(summary, separators=(",", ":"), sort_keys=True) + "\n")
    sidecar = directory / f"export-{request_id}.attestation.json"
    result = run(
        str(DR / "export-attestation.py"), "create", "--sidecar", str(sidecar),
        "--artifact", str(artifact), "--summary", str(summary_path),
        "--request-id", request_id, "--tenant-id", tenant_id, "--scope", "tenant",
        "--actor-id", "brevitas_admin:opaque", "--target-id", "staging-us",
        "--environment", "staging", "--requested-at", "2026-01-01T00:00:00Z",
        "--due-at", "2026-01-31T00:00:00Z", "--deadline-breached", "false",
        env={"BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": key},
    )
    assert result.returncode == 0, result.stderr
    return sidecar, json.loads(sidecar.read_text())


def write_fake_restore_tools(directory: Path) -> Path:
    log = directory / "psql.log"
    write_executable(directory / "psql", """#!/usr/bin/env python3
import os
import pathlib
import sys

arguments = sys.argv[1:]
if any(value.startswith("--set=") for value in arguments) \
        and any(value in {"-c", "--command"} or value.startswith("--command=")
                for value in arguments):
    print("parameterized SQL passed through non-interpolating psql command mode", file=sys.stderr)
    raise SystemExit(97)
sql = " ".join(arguments) + " " + sys.stdin.read()
log = os.environ.get("FAKE_PSQL_LOG")
if log:
    with pathlib.Path(log).open("a") as output:
        output.write(sql + "\\n")
manifest_hash = os.environ.get("FAKE_MANIFEST_SHA", "")
artifact_hash = os.environ.get("FAKE_DELETION_SHA", "")
if "current_database()" in sql and "raw_verified_at is null" in sql:
    print(os.environ.get("FAKE_RESTORE_PREFLIGHT", ""))
elif "pg_extension where extname" in sql and "raw_verified_at is not null" in sql:
    print("|".join(["restore_drill", "160000", "2", "3", "ephemeral-postgres",
          "restore-drill", "staging", "restore_drill", "production-us", "production",
          manifest_hash, artifact_hash, "evidence:deletions:immutable:001", "f", "f", "f"]))
elif "SELECT count(*) FROM" in sql:
    print("1")
elif "set raw_verified_at=clock_timestamp()" in sql:
    print("1")
elif "current_database(),current_setting('server_version_num')" in sql:
    print("|".join(["restore_drill", "160000", "ephemeral-postgres", "restore-drill",
          "staging", "restore_drill", "production-us", "production", manifest_hash,
          artifact_hash, "evidence:deletions:immutable:001", "t", "t", "t"]))
elif "select count(*) from brevitas_restore.replay_evidence" in sql and "raw_verified_at" not in sql:
    print("0")
elif "set replay_verified_at=coalesce" in sql:
    print("1")
elif "select (raw_verified_at is not null),(replay_verified_at is not null),(ready_at is not null),(select count(*)" in sql:
    print("t|t|f|0")
elif "set ready_at=clock_timestamp()" in sql:
    print("1")
elif "select (raw_verified_at is not null),(replay_verified_at is not null),(ready_at is not null)" in sql:
    print("t|t|t")
else:
    print("unexpected fake psql query", file=sys.stderr)
    sys.exit(9)
""")
    write_executable(directory / "age", "#!/bin/sh\nexit 0\n")
    write_executable(directory / "pg_restore", """#!/bin/sh
if [ "${1-}" = "--version" ]; then
  echo 'pg_restore (PostgreSQL) 16.9'
fi
exit 0
""")
    return log


def test_libpq_launcher_decomposes_uri_without_consuming_stdin_or_propagating_it(tmp_path):
    probe = tmp_path / "probe"
    write_executable(probe, """#!/usr/bin/env python3
import json
import os
import sys

document = {
    "argv": sys.argv[1:],
    "database": os.environ.get("PGDATABASE"),
    "host": os.environ.get("PGHOST"),
    "port": os.environ.get("PGPORT"),
    "user": os.environ.get("PGUSER"),
    "password_ok": os.environ.get("PGPASSWORD") == "p@ss:word",
    "sslmode": os.environ.get("PGSSLMODE"),
    "application_name": os.environ.get("PGAPPNAME"),
    "connect_timeout": os.environ.get("PGCONNECT_TIMEOUT"),
    "source_url_propagated": "DATABASE_URL_UNDER_TEST" in os.environ,
    "service_propagated": "PGSERVICE" in os.environ,
    "hostaddr_propagated": "PGHOSTADDR" in os.environ,
    "passfile_propagated": "PGPASSFILE" in os.environ,
    "localedir_propagated": "PGLOCALEDIR" in os.environ,
    "stdin": sys.stdin.read(),
}
print(json.dumps(document, sort_keys=True))
""")
    database_url = (
        "postgresql://user%2Bname:p%40ss%3Aword@db.example.test:6543/"
        "sample%2Ddb?sslmode=require&application_name=dr-test"
    )
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, database_url.encode())
    finally:
        os.close(write_fd)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_URL_UNDER_TEST": database_url,
        "PGHOST": "inherited-host-must-not-win",
        "PGHOSTADDR": "192.0.2.1",
        "PGPORT": "9999",
        "PGUSER": "inherited-user-must-not-win",
        "PGPASSWORD": "inherited-password-must-not-win",
        "PGPASSFILE": "/tmp/inherited-pgpass-must-not-win",
        "PGLOCALEDIR": "/tmp/inherited-locale-must-not-win",
        "PGCONNECT_TIMEOUT": "60",
        "PGSERVICE": "inherited-service-must-not-win",
    }
    try:
        result = subprocess.run(
            [sys.executable, str(DR / "libpq-exec.py"),
             "--database-url-fd", str(read_fd), "--connect-timeout", "10", "--",
             str(probe), "marker"],
            cwd=ROOT, env=environment, input="stream-intact\n",
            pass_fds=(read_fd,), capture_output=True, text=True, check=False,
        )
    finally:
        os.close(read_fd)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "application_name": "dr-test",
        "argv": ["marker"],
        "connect_timeout": "10",
        "database": "sample-db",
        "host": "db.example.test",
        "hostaddr_propagated": False,
        "localedir_propagated": False,
        "password_ok": True,
        "passfile_propagated": False,
        "port": "6543",
        "service_propagated": False,
        "source_url_propagated": False,
        "sslmode": "require",
        "stdin": "stream-intact\n",
        "user": "user+name",
    }


def test_libpq_launcher_rejects_socket_fallback_and_identity_query_overrides(tmp_path):
    marker = tmp_path / "executed"
    probe = tmp_path / "probe"
    write_executable(probe, f"#!/bin/sh\ntouch '{marker}'\n")
    invalid_urls = [
        "postgresql:///database",
        "postgresql://db.example.test:5432/database",
        "postgresql://user@db.example.test:5432/database",
        "postgresql://user:password@db.example.test/database",
        "postgresql://user:password@first%2Csecond:5432/database",
        "postgresql://user:password@db.example.test:5432/database?host=elsewhere.example.test",
        "postgresql://user:password@db.example.test:5432/database?hostaddr=127.0.0.1",
        "postgresql://user:password@db.example.test:5432/database?connect_timeout=60",
    ]
    for database_url in invalid_urls:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, database_url.encode())
        finally:
            os.close(write_fd)
        try:
            result = subprocess.run(
                [sys.executable, str(DR / "libpq-exec.py"),
                 "--database-url-fd", str(read_fd), "--connect-timeout", "10", "--",
                 str(probe)],
                cwd=ROOT, env={"PATH": os.environ.get("PATH", "")},
                pass_fds=(read_fd,), capture_output=True, text=True, check=False,
            )
        finally:
            os.close(read_fd)
        assert result.returncode == 2
        assert "invalid PostgreSQL database URL" in result.stderr
    assert not marker.exists()


def test_parameterized_dr_psql_never_uses_non_interpolating_command_mode():
    for path in [DR / "retention.sh", DR / "tenant-data.sh", DR / "verify-logical.sh"]:
        logical_commands = []
        current = ""
        for line in path.read_text().splitlines():
            current += line + "\n"
            if line.rstrip().endswith("\\"):
                continue
            logical_commands.append(current)
            current = ""
        for command in logical_commands:
            if "--set=" in command:
                assert not re.search(r"(?:^|\s)(?:-c|--command)(?:\s|=|$)", command), (
                    f"{path.name} sends psql variables through non-interpolating command mode"
                )

    replay_source = (DR / "replay-deletion-artifact.sh").read_text()
    for match in re.finditer(r'\["psql"(?P<arguments>.*?)\]', replay_source, re.DOTALL):
        arguments = match.group("arguments")
        if "--set=" in arguments:
            assert not re.search(r'"(?:-c|--command)(?:=|"|\s)', arguments), (
                "embedded replay psql sends variables through non-interpolating command mode"
            )


def test_dr_scripts_are_executable_syntax_valid_and_default_to_offline_dry_run(tmp_path):
    shell_scripts = [
        DR / "backup-logical.sh",
        DR / "bootstrap-restore-target.sh",
        DR / "export-deletion-artifact.sh",
        DR / "replay-deletion-artifact.sh",
        DR / "retention.sh",
        DR / "restore-logical.sh",
        DR / "verify-logical.sh",
        DR / "tenant-data.sh",
    ]
    for script in shell_scripts:
        assert script.stat().st_mode & 0o111
        syntax = run("bash", "-n", str(script))
        assert syntax.returncode == 0, syntax.stderr
        source = script.read_text()
        assert "set -x" not in source
        assert 'mode="dry-run"' in source
    backup_source = (DR / "backup-logical.sh").read_text()
    assert "pg_export_snapshot" in backup_source
    assert '--snapshot="$snapshot"' in backup_source
    assert "--schema=public --schema=auth" in backup_source
    for restore_contract_field in {
        '"target_contract": "brevitas-ephemeral-postgres-v1"',
        '"postgresql_major": 16', '"required_extensions": ["pgcrypto", "vector"]',
        '"required_roles": ["anon", "authenticated", "service_role"]',
    }:
        assert restore_contract_field in backup_source
    verify_source = (DR / "verify-logical.sh").read_text()
    for field in {
        '"backup_source_id"', '"source_environment"', '"destination_id"',
        '"destination_environment"', '"backup_evidence_reference"',
        '"expected_manifest_sha256"',
    }:
        assert field in verify_source
    replay_source = (DR / "replay-deletion-artifact.sh").read_text()
    assert "except subprocess.CalledProcessError:" in replay_source
    assert 'raise SystemExit("ERROR: restore control/evidence preflight mismatch")' in replay_source
    common_source = (DR / "common.sh").read_text()
    restore_source = (DR / "restore-logical.sh").read_text()
    assert "dr_require_postgresql_client_major pg_dump 16" in backup_source
    assert "dr_require_postgresql_client_major pg_restore 16" in restore_source
    assert "PostgreSQL 16 restore contract" in common_source

    backup = run(
        str(DR / "backup-logical.sh"), "--environment", "staging", "--source-id", "staging-us",
        "--output-dir", str(tmp_path / "never-created"), "--dry-run",
        env={"BACKUP_DATABASE_URL": "must-not-be-read-or-printed"},
    )
    assert backup.returncode == 0
    assert "no database connection" in backup.stderr
    assert "must-not-be-read-or-printed" not in backup.stdout + backup.stderr
    assert not (tmp_path / "never-created").exists()

    restore = run(
        str(DR / "restore-logical.sh"), "--environment", "staging", "--target-id", "restore-drill",
        "--source-environment", "production", "--source-id", "production-us",
        "--manifest", "not-opened.json", "--encrypted-backup", "not-opened.age",
        "--target-mode", "ephemeral-postgres", "--expected-database-name", "restore_drill",
        "--expected-manifest-sha256", "0" * 64,
        "--backup-evidence-reference", "evidence:restore:001",
        "--deletion-artifact", "not-opened-deletions.json",
        "--expected-deletion-artifact-sha256", "1" * 64,
        "--deletion-evidence-reference", "evidence:deletions:001",
        "--evidence-dir", str(tmp_path / "never-created"), "--dry-run",
        env={"RESTORE_DATABASE_URL": "must-not-be-read-or-printed"},
    )
    assert restore.returncode == 0
    assert "bootstrapped PostgreSQL 16 destination" in restore.stderr
    assert "mark readiness only after both verifications" in restore.stderr
    assert "must-not-be-read-or-printed" not in restore.stdout + restore.stderr


def test_production_restore_backup_and_retention_refuse_without_separate_opt_in(tmp_path):
    commands = [
        [str(DR / "backup-logical.sh"), "--environment", "production", "--source-id", "prod-us",
         "--output-dir", str(tmp_path), "--dry-run"],
        [str(DR / "restore-logical.sh"), "--environment", "production", "--target-id", "prod-restore",
         "--target-mode", "ephemeral-postgres", "--expected-database-name", "prod_restore",
         "--source-environment", "production", "--source-id", "prod-source",
         "--manifest", "x", "--encrypted-backup", "y",
         "--expected-manifest-sha256", "0" * 64,
         "--backup-evidence-reference", "evidence:production:001",
         "--deletion-artifact", "z", "--expected-deletion-artifact-sha256", "1" * 64,
         "--deletion-evidence-reference", "evidence:deletions:prod:001",
         "--evidence-dir", str(tmp_path), "--dry-run"],
        [str(DR / "verify-logical.sh"), "--environment", "production", "--target-id", "prod-restore",
         "--target-mode", "ephemeral-postgres", "--expected-database-name", "prod_restore",
         "--source-environment", "production", "--source-id", "prod-source",
         "--manifest", "x", "--encrypted-backup", "y",
         "--expected-manifest-sha256", "0" * 64,
         "--backup-evidence-reference", "evidence:production:001",
         "--deletion-artifact", "z", "--expected-deletion-artifact-sha256", "1" * 64,
         "--deletion-evidence-reference", "evidence:deletions:prod:001",
         "--evidence-dir", str(tmp_path), "--dry-run"],
        [str(DR / "prune-logical-backups.py"), "--environment", "production", "--source-id", "prod-us",
         "--backup-dir", str(tmp_path)],
    ]
    for command in commands:
        result = run(*command)
        assert result.returncode != 0
        assert "production is refused by default" in result.stderr


def test_mutating_modes_require_exact_confirmation_before_reading_credentials(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000001"
    tenant_id = "00000000-0000-4000-8000-000000000002"
    commands = [
        [str(DR / "backup-logical.sh"), "--environment", "staging", "--source-id", "staging-us",
         "--output-dir", str(tmp_path / "backup"), "--apply", "--confirm", "wrong"],
        [str(DR / "restore-logical.sh"), "--environment", "staging", "--target-id", "restore-id",
         "--target-mode", "ephemeral-postgres", "--expected-database-name", "restore_id",
         "--source-environment", "production", "--source-id", "prod-source",
         "--manifest", "x", "--encrypted-backup", "y", "--evidence-dir", str(tmp_path),
         "--expected-manifest-sha256", "0" * 64,
         "--backup-evidence-reference", "evidence:restore:002",
         "--deletion-artifact", "z", "--expected-deletion-artifact-sha256", "1" * 64,
         "--deletion-evidence-reference", "evidence:deletions:002",
         "--apply", "--confirm", "wrong"],
        [str(DR / "tenant-data.sh"), "--action", "delete", "--request-id", request_id,
         "--tenant-id", tenant_id, "--environment", "staging", "--target-id", "staging-us",
         "--evidence-dir", str(tmp_path), "--actor-id", "brevitas_admin:opaque",
         "--apply", "--confirm", "wrong"],
    ]
    for command in commands:
        result = run(*command, env={
            "BACKUP_DATABASE_URL": "must-not-be-read-or-printed",
            "RESTORE_DATABASE_URL": "must-not-be-read-or-printed",
            "COMPLIANCE_DATABASE_URL": "must-not-be-read-or-printed",
        })
        assert result.returncode != 0
        assert "confirmation mismatch" in result.stderr
        assert "must-not-be-read-or-printed" not in result.stdout + result.stderr


def test_restore_and_verify_bind_independent_manifest_hash_source_and_nonempty_inventory(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backup, manifest, _, manifest_sha256 = write_backup_set(
        tmp_path, source="production-us", environment="production",
        created_at=now - dt.timedelta(days=1),
    )
    deletion_artifact, deletion_sha256 = write_deletion_artifact(
        tmp_path, source="production-us", environment="production",
        manifest_sha256=manifest_sha256, issued_at=now,
    )
    base = [
        "--environment", "staging", "--target-id", "restore-drill",
        "--target-mode", "ephemeral-postgres", "--expected-database-name", "restore_drill",
        "--source-environment", "production", "--source-id", "production-us",
        "--manifest", str(manifest), "--encrypted-backup", str(backup),
        "--backup-evidence-reference", "evidence:backup:immutable:001",
        "--deletion-artifact", str(deletion_artifact),
        "--expected-deletion-artifact-sha256", deletion_sha256,
        "--deletion-evidence-reference", "evidence:deletions:immutable:001",
        "--evidence-dir", str(tmp_path / "evidence"), "--apply",
    ]
    for script, operation in (("restore-logical.sh", "RESTORE"), ("verify-logical.sh", "VERIFY")):
        wrong_hash = run(
            str(DR / script), *base,
            "--expected-manifest-sha256", "0" * 64,
            "--confirm", f"{operation}:production-us:restore-drill",
            env={"RESTORE_DATABASE_URL": "must-not-be-read-or-printed"},
        )
        assert wrong_hash.returncode != 0
        assert "independent expected SHA-256" in wrong_hash.stderr
        assert "must-not-be-read-or-printed" not in wrong_hash.stdout + wrong_hash.stderr

        mismatched_source = [
            "different-source" if value == "production-us" else value for value in base
        ]
        mismatch = run(
            str(DR / script), *mismatched_source,
            "--expected-manifest-sha256", manifest_sha256,
            "--confirm", f"{operation}:different-source:restore-drill",
        )
        assert mismatch.returncode != 0
        assert "manifest source identity" in mismatch.stderr

    empty_document = json.loads(manifest.read_text())
    empty_document["tables"] = []
    manifest.write_text(json.dumps(empty_document, indent=2, sort_keys=True) + "\n")
    empty_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    empty = run(
        str(DR / "restore-logical.sh"), *base,
        "--expected-manifest-sha256", empty_hash,
        "--confirm", "RESTORE:production-us:restore-drill",
    )
    assert empty.returncode != 0
    assert "manifest table inventory is empty" in empty.stderr


def test_restore_requires_exact_bootstrap_control_before_decrypting(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backup, manifest, _, manifest_sha256 = write_backup_set(
        tmp_path, source="production-us", environment="production",
        created_at=now - dt.timedelta(days=1),
    )
    artifact, artifact_sha256 = write_deletion_artifact(
        tmp_path, source="production-us", environment="production",
        manifest_sha256=manifest_sha256, issued_at=now,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = write_fake_restore_tools(fake_bin)
    arguments = [
        str(DR / "restore-logical.sh"), "--environment", "staging",
        "--target-id", "restore-drill", "--target-mode", "ephemeral-postgres",
        "--expected-database-name", "restore_drill", "--source-environment", "production",
        "--source-id", "production-us", "--manifest", str(manifest),
        "--encrypted-backup", str(backup), "--expected-manifest-sha256", manifest_sha256,
        "--backup-evidence-reference", "evidence:backup:immutable:001",
        "--deletion-artifact", str(artifact),
        "--expected-deletion-artifact-sha256", artifact_sha256,
        "--deletion-evidence-reference", "evidence:deletions:immutable:001",
        "--evidence-dir", str(tmp_path / "evidence"), "--apply", "--confirm",
        "RESTORE:production-us:restore-drill",
    ]
    base_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "RESTORE_DATABASE_URL": "postgresql://fake-user:fake-password@fake-host:5432/fake-db",
        "BREVITAS_BACKUP_AGE_IDENTITY": "restricted-fake-age-identity",
        "FAKE_PSQL_LOG": str(log),
    }
    missing = run(*arguments, env=base_env)
    assert missing.returncode != 0
    assert "database name/control mismatch" in missing.stderr
    assert "age --decrypt" not in log.read_text()

    wrong_control = "|".join([
        "wrong_database", "160000", "2", "3", "0", "ephemeral-postgres",
        "restore-drill", "staging", "wrong_database", "production-us", "production",
        manifest_sha256, artifact_sha256, "evidence:deletions:immutable:001", "t", "t", "t",
    ])
    wrong = run(*arguments, env={**base_env, "FAKE_RESTORE_PREFLIGHT": wrong_control})
    assert wrong.returncode != 0
    assert "database name/control mismatch" in wrong.stderr


def test_verify_replays_even_empty_deletion_artifact_before_readiness(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backup, manifest, _, manifest_sha256 = write_backup_set(
        tmp_path, source="production-us", environment="production",
        created_at=now - dt.timedelta(days=1),
    )
    artifact, artifact_sha256 = write_deletion_artifact(
        tmp_path, source="production-us", environment="production",
        manifest_sha256=manifest_sha256, issued_at=now, tombstones=[],
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = write_fake_restore_tools(fake_bin)
    evidence_dir = tmp_path / "evidence"
    result = run(
        str(DR / "verify-logical.sh"), "--environment", "staging",
        "--target-id", "restore-drill", "--target-mode", "ephemeral-postgres",
        "--expected-database-name", "restore_drill", "--source-environment", "production",
        "--source-id", "production-us", "--manifest", str(manifest),
        "--encrypted-backup", str(backup), "--expected-manifest-sha256", manifest_sha256,
        "--backup-evidence-reference", "evidence:backup:immutable:001",
        "--deletion-artifact", str(artifact),
        "--expected-deletion-artifact-sha256", artifact_sha256,
        "--deletion-evidence-reference", "evidence:deletions:immutable:001",
        "--evidence-dir", str(evidence_dir), "--apply", "--confirm",
        "VERIFY:production-us:restore-drill",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "RESTORE_DATABASE_URL": "postgresql://fake-user:fake-password@fake-host:5432/fake-db",
            "FAKE_PSQL_LOG": str(log),
            "FAKE_MANIFEST_SHA": manifest_sha256,
            "FAKE_DELETION_SHA": artifact_sha256,
        },
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(next(evidence_dir.glob("verify-*.json")).read_text())
    assert evidence["raw_table_counts"] == "verified"
    assert evidence["deletion_replay"] == "verified"
    assert evidence["deletion_tombstone_count"] == 0
    assert evidence["ready_after_replay"] is True
    assert evidence["readiness_scope"] == "isolated-verification-only"
    calls = log.read_text()
    assert calls.index("set replay_verified_at=coalesce") < calls.index("set ready_at=clock_timestamp()")


def test_deletion_artifact_must_be_newer_and_independently_bound(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backup, manifest, _, manifest_sha256 = write_backup_set(
        tmp_path, source="production-us", environment="production", created_at=now,
    )
    artifact, _ = write_deletion_artifact(
        tmp_path, source="production-us", environment="production",
        manifest_sha256=manifest_sha256, issued_at=now, backup_created_at=now,
    )
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    result = run(
        str(DR / "verify-logical.sh"), "--environment", "staging",
        "--target-id", "restore-drill", "--target-mode", "ephemeral-postgres",
        "--expected-database-name", "restore_drill", "--source-environment", "production",
        "--source-id", "production-us", "--manifest", str(manifest),
        "--encrypted-backup", str(backup), "--expected-manifest-sha256", manifest_sha256,
        "--backup-evidence-reference", "evidence:backup:immutable:001",
        "--deletion-artifact", str(artifact),
        "--expected-deletion-artifact-sha256", artifact_sha256,
        "--deletion-evidence-reference", "evidence:deletions:immutable:001",
        "--evidence-dir", str(tmp_path / "evidence"), "--apply", "--confirm",
        "VERIFY:production-us:restore-drill",
        env={"RESTORE_DATABASE_URL": "must-not-be-read"},
    )
    assert result.returncode != 0
    assert "deletion artifact must be newer than the backup" in result.stderr
    assert "must-not-be-read" not in result.stdout + result.stderr


def test_retention_uses_validated_manifest_time_not_copy_touch_or_mtime(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expired = write_backup_set(
        tmp_path, source="staging-us", environment="staging",
        created_at=now - dt.timedelta(days=36),
    )[:3]
    recent = write_backup_set(
        tmp_path, source="staging-us", environment="staging",
        created_at=now - dt.timedelta(days=2),
    )[:3]
    # Touch/copy metadata cannot make an old backup recent or a recent backup old.
    for path in expired:
        os.utime(path, None)
    premature_mtime = (now - dt.timedelta(days=90)).timestamp()
    for path in recent:
        os.utime(path, (premature_mtime, premature_mtime))

    dry_run = run(
        str(DR / "prune-logical-backups.py"), "--environment", "staging",
        "--source-id", "staging-us", "--backup-dir", str(tmp_path),
    )
    assert dry_run.returncode == 0
    assert "retention_days=35" in dry_run.stdout
    assert f"WOULD_DELETE {expired[0].name}" in dry_run.stdout
    assert recent[0].name not in dry_run.stdout
    assert all(path.exists() for path in (*expired, *recent))

    refused = run(
        str(DR / "prune-logical-backups.py"), "--environment", "staging",
        "--source-id", "staging-us", "--backup-dir", str(tmp_path),
        "--apply", "--confirm", "wrong",
    )
    assert refused.returncode != 0
    assert expired[0].exists()

    applied = run(
        str(DR / "prune-logical-backups.py"), "--environment", "staging",
        "--source-id", "staging-us", "--backup-dir", str(tmp_path),
        "--apply", "--confirm", "PRUNE:staging-us:35D",
    )
    assert applied.returncode == 0
    assert not any(path.exists() for path in expired)
    assert all(path.exists() for path in recent)


def test_retention_rejects_copied_or_tampered_manifest_identity(tmp_path):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    backup, manifest, evidence, _ = write_backup_set(
        tmp_path, source="staging-us", environment="staging",
        created_at=now - dt.timedelta(days=36),
    )
    document = json.loads(manifest.read_text())
    document["backup_source_id"] = "different-source"
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    result = run(
        str(DR / "prune-logical-backups.py"), "--environment", "staging",
        "--source-id", "staging-us", "--backup-dir", str(tmp_path),
        "--apply", "--confirm", "PRUNE:staging-us:35D",
    )
    assert result.returncode != 0
    assert "validation failed" in result.stderr
    assert all(path.exists() for path in (backup, manifest, evidence))


def test_tenant_workflow_is_scoped_guarded_and_fails_closed_on_missing_contract(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000001"
    tenant_id = "00000000-0000-4000-8000-000000000002"
    script = DR / "tenant-data.sh"
    dry_run = run(
        str(script), "--action", "delete", "--request-id", request_id, "--tenant-id", tenant_id,
        "--environment", "staging", "--target-id", "staging-us",
        "--evidence-dir", str(tmp_path / "never-created"), "--dry-run",
        env={"COMPLIANCE_DATABASE_URL": "must-not-be-read-or-printed"},
    )
    assert dry_run.returncode == 0
    assert "legal hold" in dry_run.stderr
    assert "35 days" in dry_run.stderr
    assert "must-not-be-read-or-printed" not in dry_run.stdout + dry_run.stderr
    assert not (tmp_path / "never-created").exists()

    source = script.read_text()
    for capability in {
        "public.data_subject_requests",
        "public.legal_holds",
        "public.backup_deletion_tombstones",
        "public.compliance_export_tenant(uuid,uuid,text)",
        "public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)",
        "public.compliance_delete_tenant(uuid,uuid,text)",
    }:
        assert capability in source
    assert "required compliance migration/RPC capabilities are absent" in source
    assert "interval '30 days'" in source
    assert "interval '35 days'" in source
    assert "general_telemetry_content_exported" in source
    assert "compliance_data_request_deadline_breached" in source
    assert "due_at >= now()" not in source
    assert "completed_at <= due_at" not in source
    assert "status<>'completed' and due_at<now()" in source
    assert source.index("verify-and-attest-export.py") < source.index("select public.compliance_complete_export")
    for hardened_export_control in {
        "BREVITAS_EXPORT_AGE_IDENTITY", "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY",
        "BREVITAS_COMPLIANCE_DECRYPT_COMMAND", "portable_record_count",
        "portable_records_sha256", "attestation_sha256",
    }:
        assert hardened_export_control in source


def test_export_resume_finalizes_existing_artifact_and_recreates_missing_evidence(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000011"
    tenant_id = "00000000-0000-4000-8000-000000000012"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_compliance_psql(fake_bin)
    write_fake_age(fake_bin)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_dir.chmod(0o700)
    artifact = evidence_dir / f"export-{request_id}.jsonl.age"
    summary = write_portable_artifact(artifact, [{
        "record_type": "organization", "data": {"id": tenant_id, "name": "Example"},
    }])
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    hmac_key = "test-hmac-key-material-that-is-at-least-thirty-two-bytes"
    sidecar, _ = create_export_attestation(
        evidence_dir, artifact, summary, request_id=request_id, tenant_id=tenant_id, key=hmac_key,
    )
    attestation_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    arguments = [
        str(DR / "tenant-data.sh"), "--action", "export", "--scope", "tenant",
        "--request-id", request_id, "--tenant-id", tenant_id,
        "--environment", "staging", "--target-id", "staging-us",
        "--evidence-dir", str(evidence_dir), "--actor-id", "brevitas_admin:opaque",
        "--apply", "--confirm", f"EXPORT:staging-us:{request_id}",
    ]
    base_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "COMPLIANCE_DATABASE_URL": "postgresql://fake-user:fake-password@fake-host:5432/fake-db",
        "FAKE_ARTIFACT_SHA": digest,
        "FAKE_ATTESTATION_SHA": attestation_sha,
        "FAKE_RECORD_COUNT": str(summary["record_count"]),
        "FAKE_RECORDS_SHA": str(summary["records_sha256"]),
        "BREVITAS_EXPORT_AGE_IDENTITY": "AGE-SECRET-KEY-TEST",
        "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": hmac_key,
    }
    processing = run(*arguments, env={**base_env, "FAKE_REQUEST_STATUS": "processing"})
    assert processing.returncode == 0, processing.stderr
    evidence_path = evidence_dir / f"export-tenant-{request_id}.evidence.json"
    evidence = json.loads(evidence_path.read_text())
    assert evidence["artifact_sha256"] == digest
    assert "RESUME: verifying signed request binding" in processing.stderr

    evidence_path.unlink()
    finalized = run(*arguments, env={**base_env, "FAKE_REQUEST_STATUS": "completed"})
    assert finalized.returncode == 0, finalized.stderr
    recreated = json.loads(evidence_path.read_text())
    assert recreated["status"] == "completed"
    assert recreated["artifact_sha256"] == digest
    assert recreated["attestation_sha256"] == attestation_sha
    assert recreated["ciphertext_only_records"] == 0

    recreated["actor_id"] = "brevitas_admin:tampered"
    evidence_path.write_text(json.dumps(recreated, indent=2, sort_keys=True) + "\n")
    conflicting = run(*arguments, env={**base_env, "FAKE_REQUEST_STATUS": "completed"})
    assert conflicting.returncode != 0
    assert "existing data-rights evidence conflicts" in conflicting.stderr


def test_export_resume_rejects_artifact_that_conflicts_with_finalized_digest(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000021"
    tenant_id = "00000000-0000-4000-8000-000000000022"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_compliance_psql(fake_bin)
    write_fake_age(fake_bin)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_dir.chmod(0o700)
    artifact = evidence_dir / f"export-{request_id}.jsonl.age"
    summary = write_portable_artifact(artifact, [{"record_type": "customer", "data": {"id": tenant_id}}])
    hmac_key = "test-hmac-key-material-that-is-at-least-thirty-two-bytes"
    create_export_attestation(
        evidence_dir, artifact, summary, request_id=request_id, tenant_id=tenant_id, key=hmac_key,
    )
    artifact.write_bytes(b"substituted-after-signing")
    result = run(
        str(DR / "tenant-data.sh"), "--action", "export", "--scope", "tenant",
        "--request-id", request_id, "--tenant-id", tenant_id,
        "--environment", "staging", "--target-id", "staging-us",
        "--evidence-dir", str(evidence_dir), "--actor-id", "brevitas_admin:opaque",
        "--apply", "--confirm", f"EXPORT:staging-us:{request_id}",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "COMPLIANCE_DATABASE_URL": "postgresql://fake-user:fake-password@fake-host:5432/fake-db",
                "FAKE_REQUEST_STATUS": "completed",
                "FAKE_ARTIFACT_SHA": "0" * 64,
                "BREVITAS_EXPORT_AGE_IDENTITY": "AGE-SECRET-KEY-TEST",
                "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": hmac_key,
        },
    )
    assert result.returncode != 0
    assert "portable export" in result.stderr or "attestation" in result.stderr


def test_export_verification_rejects_concurrent_path_substitution(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000023"
    tenant_id = "00000000-0000-4000-8000-000000000024"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(fake_bin / "age", """#!/usr/bin/env python3
import os
import pathlib
import shutil
import sys

target=pathlib.Path(os.environ["SUBSTITUTE_ARTIFACT"])
displaced=target.with_suffix(".displaced")
target.rename(displaced)
replacement=target.with_suffix(".replacement")
replacement.write_bytes(b"concurrently-substituted")
replacement.replace(target)
shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)
""")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    evidence_dir.chmod(0o700)
    artifact = evidence_dir / f"export-{request_id}.jsonl.age"
    expected_summary = write_portable_artifact(
        artifact, [{"record_type": "organization", "data": {"id": tenant_id}}],
    )
    summary_path = evidence_dir / "portable-summary.json"
    sidecar = evidence_dir / f"export-{request_id}.attestation.json"
    identity = evidence_dir / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-TEST\n")
    identity.chmod(0o600)
    result = run(
        str(DR / "verify-and-attest-export.py"),
        "--artifact", str(artifact), "--sidecar", str(sidecar),
        "--summary", str(summary_path), "--identity-file", str(identity),
        "--request-id", request_id, "--tenant-id", tenant_id, "--scope", "tenant",
        "--actor-id", "brevitas_admin:opaque", "--target-id", "staging-us",
        "--environment", "staging", "--requested-at", "2026-01-01T00:00:00Z",
        "--due-at", "2026-01-31T00:00:00Z", "--deadline-breached", "false",
        "--hmac-key-env", "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY",
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "SUBSTITUTE_ARTIFACT": str(artifact),
            "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY":
                "test-hmac-key-material-that-is-at-least-thirty-two-bytes",
        },
    )
    assert result.returncode != 0
    assert "concurrently substituted" in result.stderr
    assert json.loads(summary_path.read_text()) == expected_summary


def test_apply_workflows_reject_shared_or_symlinked_evidence_directories(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000025"
    tenant_id = "00000000-0000-4000-8000-000000000026"
    arguments = [
        str(DR / "tenant-data.sh"), "--action", "delete", "--request-id", request_id,
        "--tenant-id", tenant_id, "--environment", "staging", "--target-id", "staging-us",
        "--actor-id", "brevitas_admin:opaque", "--apply", "--confirm",
        f"DELETE:staging-us:{request_id}",
    ]
    shared = tmp_path / "shared-evidence"
    shared.mkdir()
    shared.chmod(0o755)
    symlink_target = tmp_path / "private-evidence"
    symlink_target.mkdir()
    symlink_target.chmod(0o700)
    symlink = tmp_path / "linked-evidence"
    symlink.symlink_to(symlink_target, target_is_directory=True)

    for unsafe, expected_error in (
        (shared, "owner-only, non-shared, and symlink-free"),
        (symlink, "must not be a symbolic link"),
    ):
        result = run(
            *arguments, "--evidence-dir", str(unsafe),
            env={"COMPLIANCE_DATABASE_URL": "must-not-be-read-or-printed"},
        )
        assert result.returncode != 0
        assert expected_error in result.stderr
        assert "must-not-be-read-or-printed" not in result.stdout + result.stderr


def test_portable_export_decrypts_every_encrypted_record_with_exact_context(tmp_path):
    decryptor = tmp_path / "decryptor"
    write_executable(decryptor, """#!/usr/bin/env python3
import json
import sys
request=json.load(sys.stdin)
purpose=request["context"]["purpose"]
plaintext={"decrypted":purpose}
if purpose=="provider_credential":
    plaintext="provider-secret-for-restricted-export"
response={
  "schema":"brevitas.compliance-decrypt-response.v1","status":"decrypted",
  "context_sha256":request["context_sha256"],
  "ciphertext_sha256":request["ciphertext_sha256"],"plaintext":plaintext,
}
print(json.dumps(response,separators=(",",":"),sort_keys=True))
""")
    records = [
        {"record_type": "customer", "data": {"id": "customer-1"}},
        {"record_type": "encrypted_content", "data": {
            "encryption_kind": "application_envelope", "ciphertext": "job-cipher",
            "context": {"purpose": "durable_job", "job_id": "job-1",
                        "organization_id": "org-1", "field": "payload"},
            "output_record_type": "ai_job_payload", "content_field": "payload",
            "metadata": {"id": "job-1"},
        }},
        {"record_type": "encrypted_content", "data": {
            "encryption_kind": "semantic_cache", "ciphertext": "cache-cipher",
            "context": {"purpose": "semantic-response-cache", "tenant_namespace": "tenant",
                        "exact_hash": "exact", "model_identity": "openai:model"},
            "output_record_type": "semantic_cache_content", "content_field": "response",
            "metadata": {"model_id": "openai:model"},
        }},
        {"record_type": "encrypted_content", "data": {
            "encryption_kind": "application_envelope", "ciphertext": "provider-cipher",
            "context": {"purpose": "provider_credential", "key_hash": "key-hash"},
            "output_record_type": "provider_configuration",
            "content_field": "provider_api_key", "metadata": {"provider": "openai"},
        }},
    ]
    source = "".join(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
                     for record in records)
    result = run(
        str(DR / "portable-export.py"),
        env={"BREVITAS_COMPLIANCE_DECRYPT_COMMAND": str(decryptor)}, input_text=source,
    )
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["record_type"] for record in output] == [
        "customer", "ai_job_payload", "semantic_cache_content",
        "provider_configuration", "export_integrity",
    ]
    assert output[1]["data"]["payload"] == {"decrypted": "durable_job"}
    assert output[2]["data"]["response"] == {"decrypted": "semantic-response-cache"}
    assert output[3]["data"]["provider_api_key"] == "provider-secret-for-restricted-export"
    assert '"ciphertext":' not in result.stdout.lower()

    summary_path = tmp_path / "summary.json"
    verified = run(
        str(DR / "verify-portable-export.py"), "--summary-file", str(summary_path),
        input_text=result.stdout,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(summary_path.read_text())["record_count"] == 4

    unavailable = run(str(DR / "portable-export.py"), input_text=source)
    assert unavailable.returncode != 0
    assert "managed decryption command is required" in unavailable.stderr


def test_export_attestation_rejects_tamper_and_stale_signature(tmp_path):
    request_id = "00000000-0000-4000-8000-000000000031"
    tenant_id = "00000000-0000-4000-8000-000000000032"
    artifact = tmp_path / f"export-{request_id}.jsonl.age"
    summary = write_portable_artifact(artifact, [{"record_type": "member", "data": {"id": "m"}}])
    key = "test-hmac-key-material-that-is-at-least-thirty-two-bytes"
    sidecar, original = create_export_attestation(
        tmp_path, artifact, summary, request_id=request_id, tenant_id=tenant_id, key=key,
    )
    verify_args = [
        str(DR / "export-attestation.py"), "verify", "--sidecar", str(sidecar),
        "--artifact", str(artifact), "--summary", str(tmp_path / "portable-summary.json"),
        "--request-id", request_id, "--tenant-id", tenant_id, "--scope", "tenant",
        "--actor-id", "brevitas_admin:opaque", "--target-id", "staging-us",
        "--environment", "staging", "--requested-at", "2026-01-01T00:00:00Z",
        "--due-at", "2026-01-31T00:00:00Z", "--deadline-breached", "false",
    ]
    valid = run(*verify_args, env={"BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": key})
    assert valid.returncode == 0, valid.stderr

    tampered = dict(original)
    tampered["actor_id"] = "brevitas_admin:substituted"
    sidecar.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n")
    rejected = run(*verify_args, env={"BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": key})
    assert rejected.returncode != 0
    assert "signature or request binding failed" in rejected.stderr

    stale = dict(original)
    stale["signed_at"] = "2025-01-01T00:00:00Z"
    stale["artifact_expires_at"] = "2025-01-02T00:00:00Z"
    unsigned = {name: value for name, value in stale.items() if name != "signature"}
    stale["signature"] = hmac.new(
        key.encode(), json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode(),
        hashlib.sha256,
    ).hexdigest()
    sidecar.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
    stale_result = run(*verify_args, env={"BREVITAS_EXPORT_EVIDENCE_HMAC_KEY": key})
    assert stale_result.returncode != 0
    assert "attestation is stale" in stale_result.stderr


def test_retention_job_dry_run_counts_bounds_and_apply_confirmation(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_executable(fake_bin / "psql", """#!/usr/bin/env python3
import json
import sys
arguments=sys.argv[1:]
if any(value.startswith("--set=") for value in arguments) and "-c" in arguments:
    raise SystemExit("parameterized SQL used non-interpolating command mode")
args=" ".join(arguments) + " " + sys.stdin.read()
if "to_regprocedure" in args:
    print("t")
elif "compliance_run_retention" in args:
    apply="--set=apply_value=true" in args
    result={
      "schema":"brevitas.compliance-retention-result.v1",
      "mode":"apply" if apply else "dry_run","run_id":"00000000-0000-4000-8000-000000000041",
      "batch_limit":5,"usage_candidates":2,"audit_candidates":1,"support_candidates":0,
      "requests_candidates":1,"holds_candidates":0,"prior_run_evidence_candidates":0,
      "usage_deleted":2 if apply else 0,"audit_deleted":1 if apply else 0,
      "support_deleted":0,"requests_deleted":1 if apply else 0,"holds_deleted":0,
      "prior_run_evidence_deleted":0,"idempotent_replay":False,
      "evidence_contains_customer_content":False,
    }
    print(json.dumps(result,separators=(",",":"),sort_keys=True))
else:
    raise SystemExit(9)
""")
    run_id = "00000000-0000-4000-8000-000000000041"
    evidence_dir = tmp_path / "evidence"
    arguments = [
        str(DR / "retention.sh"), "--environment", "staging", "--target-id", "staging-us",
        "--run-id", run_id, "--actor-id", "system:retention", "--batch-limit", "5",
        "--evidence-dir", str(evidence_dir),
    ]
    result = run(
        *arguments, "--dry-run",
        env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
             "COMPLIANCE_DATABASE_URL": "postgresql://fake-user:fake-password@fake-host:5432/fake-db"},
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(next(evidence_dir.glob("retention-*-dry-run.json")).read_text())
    assert evidence["policy"] == {
        "usage_months": 13, "audit_days": 400, "support_months": 24,
        "completed_request_evidence_days": 400, "financial_minimum_years": 7,
    }
    assert evidence["result"]["usage_candidates"] == 2
    assert evidence["evidence_contains_customer_content"] is False

    refused = run(
        *arguments, "--apply", "--confirm", "wrong",
        env={"COMPLIANCE_DATABASE_URL": "must-not-be-read-or-printed"},
    )
    assert refused.returncode != 0
    assert "confirmation mismatch" in refused.stderr
    assert "must-not-be-read-or-printed" not in refused.stdout + refused.stderr

    invalid_arguments = list(arguments)
    invalid_arguments[invalid_arguments.index("--batch-limit") + 1] = "10001"
    invalid = run(
        *invalid_arguments, "--dry-run",
        env={"COMPLIANCE_DATABASE_URL": "must-not-be-read-or-printed"},
    )
    assert invalid.returncode != 0
    assert "batch limit" in invalid.stderr
    source = (DR / "retention.sh").read_text()
    assert source.index('evidence="$evidence_dir/retention-') < source.index('database_url="$(dr_secret_from_env')
    assert "set(result)!=expected" in source
    assert "os.O_EXCL" in source


def test_compliance_migration_is_ordered_scoped_idempotent_and_preserves_evidence():
    migration = read("supabase/migrations/202607170007_compliance_workflows.sql")
    rollback = read("scripts/dr/202607170007_compliance_workflows.rollback.sql")
    assertions = read("scripts/dr/compliance-workflow-assertions.sql")
    assert "requires migrations through 202607170006" in migration
    for table in {
        "public.data_subject_requests", "public.legal_holds", "public.legal_hold_actions",
        "public.backup_deletion_tombstones", "public.compliance_retention_runs",
    }:
        assert f"create table if not exists {table}" in migration
        assert table in assertions
    for signature in {
        "public.compliance_submit_subject_request(uuid,uuid,text,text,uuid,text,text)",
        "public.compliance_request_legal_hold_action(uuid,uuid,text,uuid,text,text,text,text,timestamptz)",
        "public.compliance_approve_legal_hold_action(uuid,uuid,text,text)",
        "public.compliance_export_tenant(uuid,uuid,text)",
        "public.compliance_export_subject(uuid,uuid,text)",
        "public.compliance_complete_export(uuid,uuid,text,text,text,integer,text)",
        "public.compliance_delete_tenant(uuid,uuid,text)",
        "public.compliance_delete_subject(uuid,uuid,text)",
        "public.compliance_replay_deletion_tombstone(text,uuid,uuid,timestamptz,timestamptz,text,uuid,text,text,text)",
        "public.compliance_run_retention(uuid,text,integer,boolean)",
        "public.compliance_retention_worker_cycle(uuid,uuid,uuid,uuid,text,text,integer)",
        "public.compliance_retention_worker_health()",
    }:
        assert signature in migration
        assert f"grant execute on function {signature}" in migration
        assert f"drop function {signature}" in rollback
    assert "due_at <= requested_at + interval '30 days'" in migration
    assert "expires_at = request_received_at + interval '35 days'" in migration
    assert "compliance.deadline_breached" in migration
    assert "status not in ('approved', 'processing')" in migration
    assert "on conflict (request_id) do nothing" in migration
    assert "financial preservation invariant failed" in migration
    assert "subject financial preservation invariant failed" in migration
    assert "request_scope in ('tenant', 'member', 'customer')" in migration
    assert "to_regclass('public.billing_events')" in migration
    assert "to_regclass('public.legal_acceptances')" in migration
    assert "to_regclass('public.billing_accounts')" in migration
    assert "to_regclass('public.profiles')" in migration
    assert "v_placeholder_email" in migration
    assert "banned_until" in migration and "infinity" in migration
    assert "digest(p_organization_id::text || ':unattributed', 'sha256')" in migration
    assert "to_regclass('brevitas_restore.control') is null" in migration
    assert "v_raw_verified_at is null" in migration
    for export_contract in {
        "encrypted_content", "ai_job_payload", "ai_job_result",
        "semantic_cache_content", "provider_configuration", "api_key_metadata",
        "invitation_relationship", "administrative_audit_relationship",
        "device_authorization_metadata", "device_delivery_metadata",
        "authentication_identity", "authentication_session_metadata",
        "authentication_factor_metadata", "authentication_one_time_token_metadata",
        "organization_billing_relationship", "service_account_creator_relationship",
        "compliance_export_support_records(uuid)",
        "compliance_export_support_subject(uuid,text,uuid)",
        "device_fingerprint", "cache_write_5m_tokens", "cache_write_1h_tokens",
        "cache_attributable", "idempotency_key", "context_hash",
        "export_attestation_sha256", "portable_record_count", "portable_records_sha256",
    }:
        assert export_contract in migration
    for retention_contract in {
        "interval '13 months'", "interval '24 months'", "interval '400 days'",
        "p_batch_limit not between 1 and 10000", "for update skip locked",
        "support_records retention contract is unsupported",
        "audit_events_reject_update_delete", "backup_tombstones_reject_update_delete",
        "retention run idempotency conflict", "evidence_contains_customer_content",
        "usage_candidates integer not null", "prior_run_evidence_candidates integer not null",
        "compliance_preservation_hold", "compliance_global_preservation_hold",
        "legal_hold_actions_enforce_transition",
    }:
        assert retention_contract in migration
    assert "seven years subject to counsel" in migration
    assert "400-day" in migration
    assert "delete from public.billing_ledger" not in migration.lower()
    assert "delete from public.audit_events" in migration.lower()
    assert "compliance_retention_delete_immutable" in migration
    assert "from public, anon, authenticated, service_role" in migration
    assert "candidate.occurred_at < p_evidence_cutoff" in migration
    assert "order by usage.ts,usage.id limit p_batch_limit" in migration
    assert "p_batch_limit is null" in migration
    assert "p_artifact_sha256 is null" in migration
    assert "update public.organization_invitations set accepted_by=null" in migration
    assert "public.append_company_audit" in migration
    assert "from public, anon, authenticated" in migration
    assert "legacy single-actor legal hold RPC remains exposed" in assertions
    assert "grant execute on function public.compliance_create_legal_hold" not in migration
    assert "grant execute on function public.compliance_release_legal_hold" not in migration
    assert "compliance workflow rollback refused: retained evidence exists" in rollback
    for required in {
        "cross-tenant export unexpectedly succeeded", "held deletion unexpectedly succeeded",
        "invalid due date unexpectedly succeeded", "delete/tombstone is not idempotent",
        "deadline or immutable audit evidence was not preserved",
        "sole-tenant auth/profile PII was not converted to a non-login placeholder",
        "multi-organization user identity/profile was not preserved",
        "cross-tenant subject request unexpectedly succeeded",
        "pending legal hold create did not block deletion",
        "pending legal hold release weakened active hold",
        "legal hold create approval/replay/audit contract failed",
        "legal hold release approval/replay/audit contract failed",
        "legal hold action requester mutation succeeded",
        "legal hold action evidence deletion succeeded",
        "production restore replay unexpectedly succeeded without control schema",
        "tenant request UUID was reused across subject scope",
        "subject request UUID was reused as tenant scope",
        "retention dry-run counts or no-mutation guarantee failed",
        "retention apply/idempotency/hold/financial preservation failed",
        "retention worker cycle/health evidence contract failed",
        "support adapter deletion did not complete exactly",
    }:
        assert required in assertions


def test_retention_document_contains_every_exact_policy_and_telemetry_prohibition():
    policy = read("docs/compliance/RETENTION_AND_PRIVACY.md")
    exact = {
        "Never persist by default",
        "1 hour default; 24 hours maximum",
        "Disabled by default; 24 hours maximum when enabled",
        "13 months", "30 days", "400 days", "24 months",
        "Contract term plus 30 days", "7 years, subject to counsel review",
        "Supabase PITR | 14 days", "Separate encrypted logical backups | 35 days",
        "Account deletion from primary systems | Within 30 days",
        "Deletion from rotating backups | Within 35 days",
    }
    for value in exact:
        assert value in policy
    assert "Names, emails, prompts, responses" in policy
    assert "general telemetry" in policy
    assert "https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng" in policy
    assert "https://oag.ca.gov/privacy/ccpa" in policy
    for executable_control in {
        "03:15 UTC", "scripts/dr/retention.sh", "compliance_run_retention",
        "default batch is 5,000", "hard per-class cap is 10,000",
        "There is no caller-settable session bypass", "nonzero backlog\nfor more than 24 hours",
        "preservation of every usage row referenced by `billing_ledger`",
    }:
        assert executable_control in policy


def test_portable_usage_export_inventory_matches_every_safe_persisted_receipt_field():
    migration = read("supabase/migrations/202607170007_compliance_workflows.sql")
    exports = migration[
        migration.index("create or replace function public.compliance_export_tenant"):
        migration.index("create or replace function public.compliance_complete_export")
    ]
    safe_fields = {
        "id", "organization_id", "customer_id", "ts", "owner_id", "project",
        "environment", "source", "repo", "client", "agent", "call_site_id",
        "framework", "gateway", "operation", "provider", "model", "baseline_tokens",
        "optimized_tokens", "tokens_saved", "savings_pct", "fresh_input_tokens",
        "cached_input_tokens", "cache_write_tokens", "cache_write_5m_tokens",
        "cache_write_1h_tokens", "cache_attributable", "output_tokens",
        "baseline_cost_usd", "actual_cost_usd", "measured_savings_usd",
        "verified_savings_usd", "cost_saved_usd", "brevitas_fee_usd",
        "quality_proxy", "quality_status", "pricing_status", "pricing_version",
        "strategy", "receipt_source", "is_stream", "session_id", "pipeline",
        "run_id", "request_id", "authoritative",
    }
    for field in safe_fields:
        assert f"'{field}'" in exports, field
    assert "usage receipt export schema is incomplete; migration 012 is required" in migration
    assert "'usage_raw',usage.usage_raw" not in exports
    assert "'key_hash',usage.key_hash" not in exports


def test_recovery_and_incident_targets_schedules_and_evidence_are_exact():
    dr = read("docs/enterprise/DISASTER_RECOVERY.md")
    for required in {
        "15-minute critical-data RPO", "4-hour service RTO", "1-hour internal\nrestoration target",
        "14-day recovery window", "Daily at 02:15 UTC", "35 days", "Quarterly",
        "Twice yearly", "multi-zone", "TLS", "AOF every second", "Redis is coordination",
        "https://supabase.com/docs/guides/platform/backups", "table-level verification",
        "https://redis.io/docs/latest/operate/rc/databases/configuration/high-availability/",
        "https://redis.io/docs/latest/operate/rc/databases/configuration/data-persistence/",
        "ephemeral-postgres", "PostgreSQL 16", "not a fresh Supabase project",
        "deletion artifact", "including zero tombstones", "raw_verified_at", "ready_at",
    }:
        assert required in dr

    incident = read("docs/enterprise/INCIDENT_RESPONSE.md")
    for required in {
        "Within 30 minutes", "Customer updates every hour", "Within 4 hours",
        "24 hours of confirmation", "72-hour", "Company A", "Incident commander",
        "Communications lead", "no contacts here",
    }:
        assert required in incident


def test_legal_and_soc2_documents_are_unpublished_drafts_without_false_claims():
    paths = [
        "docs/compliance/README.md", "docs/compliance/DPA_DRAFT.md",
        "docs/compliance/SLA_DRAFT.md", "docs/compliance/SUBPROCESSORS_DRAFT.md",
        "docs/compliance/RETENTION_AND_PRIVACY.md", "docs/compliance/DATA_RIGHTS.md",
    ]
    for path in paths:
        text = read(path).upper()
        assert "DRAFT" in text
        assert "LEGAL" in text
        assert "NOT PUBLISHED" in text

    dpa = read("docs/compliance/DPA_DRAFT.md")
    assert "Standard Contractual Clauses" in dpa
    assert "within 24 hours after confirming" in dpa
    assert "within 72 hours" in dpa
    sla = read("docs/compliance/SLA_DRAFT.md")
    assert "99.9% monthly availability" in sla
    assert "99.95%" in sla
    assert "upstream provider outage" in sla
    assert "customer credentials" in sla
    assert "customer configuration or customer-caused failure" in sla
    assert "degrade safely" in sla

    soc2 = read("docs/enterprise/SOC2_READINESS.md")
    assert "SOC 2 readiness immediately" in soc2
    assert "SOC 2 Type I before broad enterprise production" in soc2
    assert "six-month evidence period" in soc2
    assert "do not describe Brevitas as SOC 2 certified" in soc2
    combined = "\n".join(read(path) for path in paths) + soc2
    assert "does not promise HIPAA, PCI storage, or FedRAMP support" in combined
    assert not re.search(r"Brevitas is (?:HIPAA|PCI|FedRAMP|SOC 2) (?:compliant|certified)", combined, re.I)


def test_infrastructure_template_and_setup_are_safe_repository_only_contracts():
    template = read("infra/dr/resilience-policy.template.yaml")
    assert "recovery_window_days: 14" in template
    assert "retention_days: 35" in template
    assert "critical_data_rpo_minutes: 15" in template
    assert "service_rto_hours: 4" in template
    assert "internal_restoration_target_minutes: 60" in template
    assert "aof-every-second" in template
    assert "non-authoritative-coordination-only" in template
    assert "MANAGED_KMS_KEY_REFERENCE_REQUIRED" in template
    setup = read("SUPABASE_SETUP.md")
    assert "Supabase Team or Enterprise" in setup
    assert "Supavisor pooling" in setup
    assert "14-day PITR" in setup
    assert "separate encrypted logical backup every day" in setup
    assert "retain it for 35 days" in setup


def test_new_artifacts_contain_no_literal_dsn_or_obvious_secret():
    paths = [*DR.glob("*"), *(ROOT / "docs" / "enterprise").glob("*"),
             *(ROOT / "docs" / "compliance").glob("*"), *(ROOT / "infra" / "dr").glob("*")]
    text = "\n".join(path.read_text() for path in paths if path.is_file())
    assert "postgresql://" not in text
    assert "postgres://" not in text
    assert not re.search(r"(?:sk|rk|whsec|sb_secret)_[A-Za-z0-9_-]{12,}", text)
    assert "set -x" not in text


def test_data_rights_distinguishes_tenant_member_customer_and_restore_replay():
    runbook = read("docs/compliance/DATA_RIGHTS.md")
    for required in {
        "Tenant offboarding (`tenant`)", "Member data-subject request (`member`",
        "End-customer data-subject request (`customer`", "--scope member|customer",
        "--subject-id", "cross-tenant", "non-login `auth.users` placeholder",
        "billing_events", "billing_accounts", "legal_acceptances",
        'sha256("<organization_uuid>:unattributed")', "finalization committed",
        "source/manifest-bound", "including zero tombstones", "unavailable\nin production",
        "BREVITAS_COMPLIANCE_DECRYPT_COMMAND", "BREVITAS_EXPORT_AGE_IDENTITY",
        "BREVITAS_EXPORT_EVIDENCE_HMAC_KEY", "ciphertext-only", "HMAC-SHA256",
        "durable job", "semantic-cache content", "provider configuration",
        "authentication identity", "non-secret session/MFA/one-time-token lifecycle metadata",
        "compliance_export_support_records(uuid)",
        "compliance_export_support_subject(uuid,text,uuid)",
    }:
        assert required in runbook
