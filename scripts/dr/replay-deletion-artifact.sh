#!/usr/bin/env bash

set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'EOF'
Usage: replay-deletion-artifact.sh --environment ENV --target-id ID
       --target-mode ephemeral-postgres --expected-database-name NAME
       --source-environment ENV --source-id ID
       --backup-manifest FILE --expected-manifest-sha256 SHA256
       --deletion-artifact FILE --expected-deletion-artifact-sha256 SHA256
       --deletion-evidence-reference ID --evidence-dir DIR --actor-id ID
       [--database-url-env NAME]
       [--dry-run | --apply --confirm REPLAY:SOURCE:TARGET]
       [--allow-production]
EOF
}

environment=""; target_id=""; target_mode=""; expected_database_name=""
source_environment=""; source_id=""; backup_manifest=""; expected_manifest_sha256=""
deletion_artifact=""; expected_deletion_artifact_sha256=""; deletion_evidence_reference=""
evidence_dir=""; actor_id=""; database_url_env="RESTORE_DATABASE_URL"
mode="dry-run"; confirmation=""; allow_production="false"
while (($#)); do
  case "$1" in
    --environment) environment="${2-}"; shift 2 ;;
    --target-id) target_id="${2-}"; shift 2 ;;
    --target-mode) target_mode="${2-}"; shift 2 ;;
    --expected-database-name) expected_database_name="${2-}"; shift 2 ;;
    --source-environment) source_environment="${2-}"; shift 2 ;;
    --source-id) source_id="${2-}"; shift 2 ;;
    --backup-manifest) backup_manifest="${2-}"; shift 2 ;;
    --expected-manifest-sha256) expected_manifest_sha256="${2-}"; shift 2 ;;
    --deletion-artifact) deletion_artifact="${2-}"; shift 2 ;;
    --expected-deletion-artifact-sha256) expected_deletion_artifact_sha256="${2-}"; shift 2 ;;
    --deletion-evidence-reference) deletion_evidence_reference="${2-}"; shift 2 ;;
    --evidence-dir) evidence_dir="${2-}"; shift 2 ;;
    --actor-id) actor_id="${2-}"; shift 2 ;;
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
   && -n "$backup_manifest" && -n "$expected_manifest_sha256" && -n "$deletion_artifact" \
   && -n "$expected_deletion_artifact_sha256" && -n "$deletion_evidence_reference" \
   && -n "$evidence_dir" && -n "$actor_id" ]] || { usage >&2; dr_die "all restore/replay identity and evidence arguments are required"; }
dr_validate_environment "$environment"; dr_validate_environment "$source_environment"
dr_validate_identifier "target ID" "$target_id"; dr_validate_identifier "source ID" "$source_id"
dr_validate_identifier "database name" "$expected_database_name"
[[ "$target_mode" == "ephemeral-postgres" ]] || dr_die "target mode must be ephemeral-postgres"
[[ "$source_id" != "$target_id" ]] || dr_die "source and target IDs must differ"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected manifest SHA-256 is invalid"
[[ "$expected_deletion_artifact_sha256" =~ ^[0-9a-f]{64}$ ]] || dr_die "expected deletion artifact SHA-256 is invalid"
dr_validate_identifier "deletion evidence reference" "$deletion_evidence_reference"
[[ ${#deletion_evidence_reference} -ge 8 ]] || dr_die "deletion evidence reference is too short"
[[ "$actor_id" =~ ^system:restore:[A-Za-z0-9._:-]{3,80}$ ]] || dr_die "replay actor must be an opaque system:restore identity"
dr_validate_env_name "$database_url_env"; dr_require_production_opt_in "$environment" "$allow_production"
if [[ "$mode" == "dry-run" ]]; then
  dr_note "DRY RUN: would validate and replay the source-bound deletion artifact after raw restore verification and before readiness."
  dr_note "DRY RUN: no database connection was made and no credential was read."
  exit 0
fi
dr_require_confirmation "$confirmation" "REPLAY:$source_id:$target_id"
for path in "$backup_manifest" "$deletion_artifact"; do
  [[ -f "$path" && ! -L "$path" ]] || dr_die "replay inputs must be regular non-symlink files"
done
[[ "$(dr_file_size "$backup_manifest")" -le 10485760 ]] || dr_die "backup manifest exceeds its safety bound"
[[ "$(dr_file_size "$deletion_artifact")" -le 52428800 ]] || dr_die "deletion artifact exceeds its safety bound"
[[ "$(dr_sha256 "$backup_manifest")" == "$expected_manifest_sha256" ]] || dr_die "backup manifest hash mismatch"
[[ "$(dr_sha256 "$deletion_artifact")" == "$expected_deletion_artifact_sha256" ]] || dr_die "deletion artifact hash mismatch"
dr_safe_directory "$evidence_dir"; dr_require_command python3; dr_require_command psql
database_url="$(dr_secret_from_env "$database_url_env")"
PGDATABASE="$database_url" python3 - "$backup_manifest" "$deletion_artifact" \
  "$source_id" "$source_environment" "$target_id" "$environment" "$expected_database_name" \
  "$expected_manifest_sha256" "$expected_deletion_artifact_sha256" \
  "$deletion_evidence_reference" "$actor_id" <<'PY'
import json, os, pathlib, subprocess, sys
from datetime import datetime, timezone
(
    manifest_path, artifact_path, source_id, source_environment, target_id,
    target_environment, expected_database, manifest_hash, artifact_hash,
    evidence_reference, actor_id,
) = sys.argv[1:]
manifest=json.loads(pathlib.Path(manifest_path).read_text())
artifact=json.loads(pathlib.Path(artifact_path).read_text())
if manifest.get("backup_source_id")!=source_id or manifest.get("source_environment")!=source_environment:
    raise SystemExit("ERROR: backup manifest source binding mismatch")
if manifest.get("target_contract")!="brevitas-ephemeral-postgres-v1" \
        or manifest.get("postgresql_major")!=16 \
        or manifest.get("required_extensions")!=["pgcrypto","vector"] \
        or manifest.get("required_roles")!=["anon","authenticated","service_role"]:
    raise SystemExit("ERROR: backup manifest restore target contract mismatch")
if artifact.get("schema")!="brevitas.deletion-artifact.v1" or artifact.get("backup_source_id")!=source_id or artifact.get("source_environment")!=source_environment:
    raise SystemExit("ERROR: deletion artifact source binding mismatch")
if artifact.get("backup_manifest_sha256")!=manifest_hash or artifact.get("evidence_reference")!=evidence_reference:
    raise SystemExit("ERROR: deletion artifact evidence binding mismatch")
if artifact.get("backup_created_at")!=manifest.get("created_at"):
    raise SystemExit("ERROR: deletion artifact backup timestamp binding mismatch")
backup_time=datetime.strptime(manifest["created_at"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
issued=datetime.strptime(artifact["issued_at"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
if issued<=backup_time:
    raise SystemExit("ERROR: deletion artifact is not newer than the backup")
tombstones=artifact.get("tombstones")
if not isinstance(tombstones,list):
    raise SystemExit("ERROR: deletion artifact tombstones are invalid")
dsn=os.environ.get("PGDATABASE","")
preflight=subprocess.run(
    ["psql","-X","-v","ON_ERROR_STOP=1","-At","-F","|","-c",
     "select current_database(),current_setting('server_version_num')::integer,target_mode,target_id,target_environment,expected_database_name,backup_source_id,source_environment,backup_manifest_sha256,deletion_artifact_sha256,deletion_evidence_reference,(raw_verified_at is not null),(replay_verified_at is null),(ready_at is null) from brevitas_restore.control where singleton"],
    check=True,capture_output=True,text=True,env={**os.environ,"PGDATABASE":dsn,"PGCONNECT_TIMEOUT":"10"},
).stdout.strip()
fields=preflight.split("|")
if len(fields)!=14:
    raise SystemExit("ERROR: restore control/evidence preflight mismatch")
version_num=fields[1]
if not version_num.startswith("16") or len(version_num)!=6 or not version_num.isdigit():
    raise SystemExit("ERROR: restore target requires PostgreSQL major version 16")
expected=[expected_database,"ephemeral-postgres",target_id,target_environment,expected_database,
          source_id,source_environment,manifest_hash,artifact_hash,
          evidence_reference,"t","t","t"]
if fields[:1]+fields[2:]!=expected:
    raise SystemExit("ERROR: restore control/evidence preflight mismatch")
seen=set()
for item in tombstones:
    request_id=str(item.get("request_id", "")); organization_id=str(item.get("organization_id", ""))
    scope=item.get("request_scope"); subject_id=item.get("subject_id") or ""
    if request_id in seen or scope not in {"tenant","member","customer"}:
        raise SystemExit("ERROR: duplicate or invalid deletion tombstone")
    seen.add(request_id)
    command=["psql","-X","-v","ON_ERROR_STOP=1","-qAt",
      "--set=source_id="+source_id,"--set=organization_id="+organization_id,
      "--set=request_id="+request_id,"--set=requested_at="+str(item.get("requested_at","")),
      "--set=expires_at="+str(item.get("expires_at","")),"--set=request_scope="+scope,
      "--set=subject_id="+subject_id,"--set=actor_id="+actor_id,
      "--set=evidence_reference="+evidence_reference,"--set=artifact_sha256="+artifact_hash,
      "-c","select public.compliance_replay_deletion_tombstone(:'source_id',:'organization_id'::uuid,:'request_id'::uuid,:'requested_at'::timestamptz,:'expires_at'::timestamptz,:'request_scope',nullif(:'subject_id','')::uuid,:'actor_id',:'evidence_reference',:'artifact_sha256')"]
    subprocess.run(command,check=True,capture_output=True,text=True,
                   env={**os.environ,"PGDATABASE":dsn,"PGCONNECT_TIMEOUT":"10"})
count=subprocess.run(
    ["psql","-X","-v","ON_ERROR_STOP=1","-qAt","-c","select count(*) from brevitas_restore.replay_evidence where artifact_sha256='"+artifact_hash+"'"],
    check=True,capture_output=True,text=True,env={**os.environ,"PGDATABASE":dsn,"PGCONNECT_TIMEOUT":"10"},
).stdout.strip()
if count!=str(len(tombstones)):
    raise SystemExit("ERROR: deletion replay evidence count mismatch")
marked=subprocess.run(
    ["psql","-X","-v","ON_ERROR_STOP=1","-qAt","--set=source_id="+source_id,
     "--set=manifest_hash="+manifest_hash,"--set=artifact_hash="+artifact_hash,
     "--set=evidence_reference="+evidence_reference,"-c",
     "update brevitas_restore.control set replay_verified_at=coalesce(replay_verified_at,clock_timestamp()) where singleton and raw_verified_at is not null and replay_verified_at is null and ready_at is null and backup_source_id=:'source_id' and backup_manifest_sha256=:'manifest_hash' and deletion_artifact_sha256=:'artifact_hash' and deletion_evidence_reference=:'evidence_reference' returning 1"],
    check=True,capture_output=True,text=True,env={**os.environ,"PGDATABASE":dsn,"PGCONNECT_TIMEOUT":"10"},
)
if marked.stdout.strip()!="1":
    raise SystemExit("ERROR: deletion replay verification state could not be persisted")
PY
evidence="$evidence_dir/replay-${source_id}-to-${target_id}-$(dr_timestamp).json"
[[ ! -e "$evidence" ]] || dr_die "replay evidence already exists"
python3 - "$evidence" "$source_id" "$source_environment" "$target_id" "$environment" \
  "$expected_manifest_sha256" "$expected_deletion_artifact_sha256" "$deletion_evidence_reference" <<'PY'
import json,pathlib,sys
from datetime import datetime,timezone
path,source,source_env,target,target_env,manifest_hash,artifact_hash,reference=sys.argv[1:]
document={"schema":"brevitas.deletion-replay-evidence.v1","backup_source_id":source,"source_environment":source_env,"destination_id":target,"destination_environment":target_env,"backup_manifest_sha256":manifest_hash,"deletion_artifact_sha256":artifact_hash,"deletion_evidence_reference":reference,"verified_at":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),"result":"verified"}
pathlib.Path(path).write_text(json.dumps(document,indent=2,sort_keys=True)+"\n")
PY
chmod 600 "$evidence"
dr_note "Deletion replay verified. Evidence: $evidence"
