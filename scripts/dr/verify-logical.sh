#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: verify-logical.sh --environment ENV --target-id ID
       --target-mode ephemeral-postgres --expected-database-name NAME
       --source-environment ENV --source-id ID --manifest FILE
       --encrypted-backup FILE --expected-manifest-sha256 SHA256
       --backup-evidence-reference ID --deletion-artifact FILE
       --expected-deletion-artifact-sha256 SHA256
       --deletion-evidence-reference ID --evidence-dir DIR
       [--database-url-env NAME]
       [--dry-run | --apply --confirm VERIFY:SOURCE:TARGET]
       [--allow-production]

Apply mode verifies exact raw table counts, persists raw verification, replays
the independently protected deletion artifact, verifies replay evidence, and
only then marks the isolated PostgreSQL 16 target ready.
EOF
}

environment=""; target_id=""; target_mode=""; expected_database_name=""
source_environment=""; source_id=""; manifest=""; encrypted=""
expected_manifest_sha256=""; backup_evidence_reference=""
deletion_artifact=""; expected_deletion_artifact_sha256=""
deletion_evidence_reference=""; evidence_dir=""
database_url_env="RESTORE_DATABASE_URL"; mode="dry-run"; confirmation=""
allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --target-mode) target_mode="${2-}"; shift 2 ;;
    --expected-database-name) expected_database_name="${2-}"; shift 2 ;;
    --source-environment) source_environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --manifest) manifest="${2-}"; shift 2 ;;
    --encrypted-backup) encrypted="${2-}"; shift 2 ;;
    --expected-manifest-sha256) expected_manifest_sha256="${2-}"; shift 2 ;;
    --backup-evidence-reference) backup_evidence_reference="${2-}"; shift 2 ;;
    --deletion-artifact) deletion_artifact="${2-}"; shift 2 ;;
    --expected-deletion-artifact-sha256) expected_deletion_artifact_sha256="${2-}"; shift 2 ;;
    --deletion-evidence-reference) deletion_evidence_reference="${2-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2-}"; shift 2 ;;
    --database-url-env) database_url_env="${2-}"; shift 2 ;;
    --dry-run) mode="dry-run"; shift ;;
    --apply) mode="apply"; shift ;;
    --confirm) confirmation="${2-}"; shift 2 ;;
    --allow-production) allow_production="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; dr_die "unknown or incomplete argument" ;;
  esac
done
[[ -n "$environment" && -n "$target_id" && -n "$target_mode" \
   && -n "$expected_database_name" && -n "$source_environment" && -n "$source_id" \
   && -n "$manifest" && -n "$encrypted" && -n "$expected_manifest_sha256" \
   && -n "$backup_evidence_reference" && -n "$deletion_artifact" \
   && -n "$expected_deletion_artifact_sha256" && -n "$deletion_evidence_reference" \
   && -n "$evidence_dir" ]] \
  || { usage >&2; dr_die "all source, target, artifact, hash, and evidence arguments are required"; }
dr_validate_environment "$environment"; dr_validate_environment "$source_environment"
dr_validate_identifier "target ID" "$target_id"; dr_validate_identifier "source ID" "$source_id"
dr_validate_identifier "database name" "$expected_database_name"
[[ "$target_mode" == "ephemeral-postgres" ]] || dr_die "target mode must be ephemeral-postgres"
[[ "$source_id" != "$target_id" ]] || dr_die "source ID and destination target ID must differ"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected manifest SHA-256 is invalid"
[[ "$expected_deletion_artifact_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected deletion artifact SHA-256 is invalid"
dr_validate_identifier "backup evidence reference" "$backup_evidence_reference"
dr_validate_identifier "deletion evidence reference" "$deletion_evidence_reference"
[[ ${#backup_evidence_reference} -ge 8 && ${#deletion_evidence_reference} -ge 8 ]] \
  || dr_die "evidence references must be at least eight characters"
dr_validate_env_name "$database_url_env"
dr_require_production_opt_in "$environment" "$allow_production"

if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would verify raw table counts, replay and verify the deletion artifact (including an empty tombstone set), and mark only the bound ephemeral-postgres target ready."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi

dr_require_confirmation "$confirmation" "VERIFY:$source_id:$target_id"
for path in "$manifest" "$encrypted" "$deletion_artifact"; do
  [[ -f "$path" && ! -L "$path" ]] || dr_die "backup, manifest, and deletion artifact must be regular non-symlink files"
done
[[ "$(dr_file_size "$manifest")" -le 10485760 ]] || dr_die "manifest exceeds its 10 MiB safety bound"
[[ "$(dr_file_size "$deletion_artifact")" -le 52428800 ]] || dr_die "deletion artifact exceeds its 50 MiB safety bound"
dr_safe_directory "$evidence_dir"; dr_require_command python3
actual_manifest_sha256="$(dr_sha256 "$manifest")"
[[ "$actual_manifest_sha256" == "$expected_manifest_sha256" ]] || dr_die "manifest does not match the independent expected SHA-256"
actual_deletion_sha256="$(dr_sha256 "$deletion_artifact")"
[[ "$actual_deletion_sha256" == "$expected_deletion_artifact_sha256" ]] || dr_die "deletion artifact does not match the independent expected SHA-256"
actual_ciphertext_sha256="$(dr_sha256 "$encrypted")"
metadata="$(mktemp "${TMPDIR:-/tmp}/brevitas-verify-metadata.XXXXXX")"
counts="$(mktemp "${TMPDIR:-/tmp}/brevitas-verify-counts.XXXXXX")"
trap 'rm -f -- "$metadata" "$counts"' EXIT
python3 - "$manifest" "$encrypted" "$actual_ciphertext_sha256" "$deletion_artifact" \
  "$source_id" "$source_environment" "$expected_manifest_sha256" \
  "$deletion_evidence_reference" "$metadata" <<'PY'
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

(manifest_path, encrypted_path, ciphertext_hash, artifact_path, source_id,
 source_environment, manifest_hash, deletion_reference, metadata_path) = sys.argv[1:]
manifest = json.loads(pathlib.Path(manifest_path).read_text())
if manifest.get("schema") != "brevitas.logical-backup-manifest.v2":
    raise SystemExit("ERROR: unsupported manifest schema")
if manifest.get("target_contract") != "brevitas-ephemeral-postgres-v1" \
        or manifest.get("postgresql_major") != 16 \
        or manifest.get("required_extensions") != ["pgcrypto", "vector"] \
        or manifest.get("required_roles") != ["anon", "authenticated", "service_role"]:
    raise SystemExit("ERROR: manifest restore target contract is unsupported")
if manifest.get("backup_source_id") != source_id or manifest.get("source_environment") != source_environment:
    raise SystemExit("ERROR: manifest source identity does not match operator intent")
if pathlib.Path(encrypted_path).name != manifest.get("ciphertext_file") \
        or ciphertext_hash != manifest.get("ciphertext_sha256") \
        or os.path.getsize(encrypted_path) != manifest.get("ciphertext_bytes"):
    raise SystemExit("ERROR: ciphertext integrity mismatch")
tables = manifest.get("tables")
if not isinstance(tables, list) or not tables:
    raise SystemExit("ERROR: manifest table inventory is empty")
seen = set()
for item in tables:
    schema, table, rows = item.get("schema", ""), item.get("table", ""), item.get("rows")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) \
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise SystemExit("ERROR: unsafe table identifier in manifest")
    if (schema, table) in seen or not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        raise SystemExit("ERROR: invalid or duplicate table inventory")
    seen.add((schema, table))
artifact = json.loads(pathlib.Path(artifact_path).read_text())
if artifact.get("schema") != "brevitas.deletion-artifact.v1" \
        or artifact.get("backup_source_id") != source_id \
        or artifact.get("source_environment") != source_environment:
    raise SystemExit("ERROR: deletion artifact source binding mismatch")
if artifact.get("backup_manifest_sha256") != manifest_hash \
        or artifact.get("evidence_reference") != deletion_reference:
    raise SystemExit("ERROR: deletion artifact evidence binding mismatch")
if artifact.get("backup_created_at") != manifest.get("created_at"):
    raise SystemExit("ERROR: deletion artifact backup timestamp binding mismatch")
try:
    backup_time = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    issued_time = datetime.strptime(artifact["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
except (KeyError, TypeError, ValueError):
    raise SystemExit("ERROR: restore artifact timestamp is invalid")
if issued_time <= backup_time:
    raise SystemExit("ERROR: deletion artifact must be newer than the backup")
tombstones = artifact.get("tombstones")
if not isinstance(tombstones, list):
    raise SystemExit("ERROR: deletion artifact tombstones are invalid")
pathlib.Path(metadata_path).write_text(json.dumps({
    "table_count": len(tables), "tombstone_count": len(tombstones)
}))
PY
tombstone_count="$(python3 - "$metadata" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())["tombstone_count"])
PY
)"

dr_require_command psql
database_url="$(dr_secret_from_env "$database_url_env")"
preflight="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select current_database(),current_setting('server_version_num')::integer,(select count(*) from pg_extension where extname in ('pgcrypto','vector')),(select count(*) from pg_roles where rolname in ('anon','authenticated','service_role')),target_mode,target_id,target_environment,expected_database_name,backup_source_id,source_environment,backup_manifest_sha256,deletion_artifact_sha256,deletion_evidence_reference,(raw_verified_at is not null),(replay_verified_at is not null),(ready_at is not null) from brevitas_restore.control where singleton")"
IFS='|' read -r actual_database version_num extension_count role_count control_mode \
  control_target control_environment control_database control_source control_source_environment control_manifest \
  control_deletion control_reference raw_verified replay_verified ready <<< "$preflight"
[[ "$actual_database" == "$expected_database_name" && "$control_database" == "$expected_database_name" ]] || dr_die "restore target database name/control mismatch"
[[ "$version_num" =~ ^16[0-9]{4}$ ]] || dr_die "restore target requires PostgreSQL major version 16"
[[ "$extension_count" == "2" && "$role_count" == "3" ]] || dr_die "restore target is missing required extensions or roles"
[[ "$control_mode" == "$target_mode" && "$control_target" == "$target_id" \
   && "$control_environment" == "$environment" \
   && "$control_source" == "$source_id" && "$control_source_environment" == "$source_environment" \
   && "$control_manifest" == "$expected_manifest_sha256" \
   && "$control_deletion" == "$expected_deletion_artifact_sha256" \
   && "$control_reference" == "$deletion_evidence_reference" ]] \
  || dr_die "restore control/evidence preflight mismatch"

if [[ "$raw_verified" != "t" ]]; then
  [[ "$replay_verified" == "f" && "$ready" == "f" ]] || dr_die "restore state is inconsistent before raw verification"
  PGDATABASE="$database_url" python3 - "$manifest" "$counts" <<'PY'
import json
import os
import pathlib
import re
import subprocess
import sys

manifest_path, counts_path = sys.argv[1:]
dsn = os.environ.get("PGDATABASE", "")
if not dsn:
    raise SystemExit("ERROR: database credential is unavailable")
document = json.loads(pathlib.Path(manifest_path).read_text())
with pathlib.Path(counts_path).open("w") as output:
    for item in document["tables"]:
        schema, table = item["schema"], item["table"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) \
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise SystemExit("ERROR: unsafe table identifier in manifest")
        result = subprocess.run(
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c",
             f'SELECT count(*) FROM "{schema}"."{table}"'],
            check=True, capture_output=True, text=True,
            env={**os.environ, "PGDATABASE": dsn, "PGCONNECT_TIMEOUT": "10"},
        )
        count = result.stdout.strip()
        if not count.isdigit():
            raise SystemExit("ERROR: invalid table count")
        output.write(f"{schema}\t{table}\t{count}\n")
expected = {(x["schema"], x["table"]): x["rows"] for x in document["tables"]}
actual = {}
for line in pathlib.Path(counts_path).read_text().splitlines():
    schema, table, count = line.split("\t")
    actual[(schema, table)] = int(count)
mismatches = [f"{s}.{t}" for (s, t), count in expected.items() if actual.get((s, t)) != count]
if set(actual) != set(expected):
    mismatches.append("table-set")
if mismatches:
    raise SystemExit("ERROR: restored raw table verification failed: " + ",".join(sorted(mismatches)))
PY
  updated="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -qAt \
    --set=target_id="$target_id" --set=manifest_hash="$expected_manifest_sha256" \
    --set=artifact_hash="$expected_deletion_artifact_sha256" -c \
    "update brevitas_restore.control set raw_verified_at=clock_timestamp() where singleton and target_id=:'target_id' and backup_manifest_sha256=:'manifest_hash' and deletion_artifact_sha256=:'artifact_hash' and raw_verified_at is null and replay_verified_at is null and ready_at is null returning 1")"
  [[ "$updated" == "1" ]] || dr_die "raw verification state could not be persisted"
  raw_verified="t"
fi

if [[ "$replay_verified" != "t" ]]; then
  replay_args=(
    --environment "$environment" --target-id "$target_id"
    --target-mode "$target_mode" --expected-database-name "$expected_database_name"
    --source-environment "$source_environment" --source-id "$source_id"
    --backup-manifest "$manifest" --expected-manifest-sha256 "$expected_manifest_sha256"
    --deletion-artifact "$deletion_artifact"
    --expected-deletion-artifact-sha256 "$expected_deletion_artifact_sha256"
    --deletion-evidence-reference "$deletion_evidence_reference"
    --evidence-dir "$evidence_dir" --actor-id "system:restore:replay"
    --database-url-env "$database_url_env" --apply --confirm "REPLAY:$source_id:$target_id"
  )
  if [[ "$allow_production" == "true" ]]; then replay_args+=(--allow-production); fi
  "$SCRIPT_DIR/replay-deletion-artifact.sh" "${replay_args[@]}"
fi

post_replay="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -At -F '|' \
  --set=artifact_hash="$expected_deletion_artifact_sha256" -c \
  "select (raw_verified_at is not null),(replay_verified_at is not null),(ready_at is not null),(select count(*) from brevitas_restore.replay_evidence where artifact_sha256=:'artifact_hash') from brevitas_restore.control where singleton")"
IFS='|' read -r raw_verified replay_verified ready replay_count <<< "$post_replay"
[[ "$raw_verified" == "t" && "$replay_verified" == "t" ]] || dr_die "deletion replay was not durably verified"
[[ "$replay_count" == "$tombstone_count" ]] || dr_die "deletion replay evidence count mismatch"
if [[ "$ready" != "t" ]]; then
  ready_update="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -qAt \
    --set=target_id="$target_id" --set=manifest_hash="$expected_manifest_sha256" \
    --set=artifact_hash="$expected_deletion_artifact_sha256" -c \
    "update brevitas_restore.control set ready_at=clock_timestamp() where singleton and target_id=:'target_id' and backup_manifest_sha256=:'manifest_hash' and deletion_artifact_sha256=:'artifact_hash' and raw_verified_at is not null and replay_verified_at is not null and ready_at is null returning 1")"
  [[ "$ready_update" == "1" ]] || dr_die "restore readiness could not be persisted after deletion replay"
fi

final_state="$(PGDATABASE="$database_url" PGCONNECT_TIMEOUT=10 psql -X -v ON_ERROR_STOP=1 -At -F '|' -c \
  "select (raw_verified_at is not null),(replay_verified_at is not null),(ready_at is not null) from brevitas_restore.control where singleton")"
[[ "$final_state" == "t|t|t" ]] || dr_die "restore target did not reach verified readiness"

evidence="$evidence_dir/verify-${target_id}-$(dr_timestamp).json"
[[ ! -e "$evidence" ]] || dr_die "verification evidence already exists; refusing to overwrite"
python3 - "$manifest" "$metadata" "$evidence" "$source_id" "$source_environment" \
  "$target_id" "$environment" "$target_mode" "$expected_database_name" \
  "$backup_evidence_reference" "$expected_manifest_sha256" \
  "$expected_deletion_artifact_sha256" "$deletion_evidence_reference" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

(manifest_path, metadata_path, evidence_path, source, source_environment,
 destination, destination_environment, target_mode, database_name,
 backup_reference, manifest_hash, artifact_hash, deletion_reference) = sys.argv[1:]
manifest = json.loads(pathlib.Path(manifest_path).read_text())
metadata = json.loads(pathlib.Path(metadata_path).read_text())
document = {
    "schema": "brevitas.restore-verification-evidence.v3",
    "backup_source_id": source,
    "source_environment": source_environment,
    "destination_id": destination,
    "destination_environment": destination_environment,
    "target_mode": target_mode,
    "expected_database_name": database_name,
    "backup_evidence_reference": backup_reference,
    "expected_manifest_sha256": manifest_hash,
    "deletion_artifact_sha256": artifact_hash,
    "deletion_evidence_reference": deletion_reference,
    "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ciphertext_integrity": "verified",
    "raw_table_counts": "verified",
    "raw_table_count": metadata["table_count"],
    "deletion_replay": "verified",
    "deletion_tombstone_count": metadata["tombstone_count"],
    "ready_after_replay": True,
    "readiness_scope": "isolated-verification-only",
    "restore_contract": manifest["target_contract"],
    "evidence_contains_customer_content": False,
}
pathlib.Path(evidence_path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
chmod 600 "$evidence"
dr_note "Raw restore and deletion replay verified; target is ready for isolated verification only. Evidence: $evidence"
