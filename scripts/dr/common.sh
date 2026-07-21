#!/usr/bin/env bash

# Shared safety primitives for disaster-recovery commands. This file is sourced.
set -Eeuo pipefail
umask 077

dr_die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 2
}

dr_note() {
  printf '%s\n' "$1" >&2
}

dr_require_command() {
  command -v "$1" >/dev/null 2>&1 || dr_die "required command is unavailable: $1"
}

dr_require_postgresql_client_major() {
  local command_name="$1"
  local expected_major="$2"
  local version
  local actual_major
  dr_require_command "$command_name"
  version="$("$command_name" --version 2>/dev/null)" \
    || dr_die "unable to identify PostgreSQL client: $command_name"
  if [[ "$version" =~ PostgreSQL\)[[:space:]]+([0-9]+) ]]; then
    actual_major="${BASH_REMATCH[1]}"
  else
    dr_die "unable to parse PostgreSQL client version: $command_name"
  fi
  [[ "$actual_major" == "$expected_major" ]] \
    || dr_die "$command_name major version must be $expected_major for the PostgreSQL 16 restore contract"
}

dr_validate_environment() {
  case "$1" in
    local|test|development|staging|production) ;;
    *) dr_die "--environment must be local, test, development, staging, or production" ;;
  esac
}

dr_validate_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z0-9._:-]{3,128}$ ]] || dr_die "$label must be an opaque 3-128 character identifier"
  [[ "$value" != *"@"* ]] || dr_die "$label must not contain an email address"
}

dr_validate_uuid() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$ ]] || dr_die "$label must be a UUID"
}

dr_validate_env_name() {
  [[ "$1" =~ ^[A-Z][A-Z0-9_]{2,63}$ ]] || dr_die "credential environment variable name is invalid"
}

dr_secret_from_env() {
  local name="$1"
  dr_validate_env_name "$name"
  local value="${!name-}"
  [[ -n "$value" ]] || dr_die "required credential environment variable is unset"
  printf '%s' "$value"
}

dr_database_exec() {
  # Descriptor 3 carries the credential without consuming the database
  # command's stdin and without placing a password-bearing URI in argv.
  local database_url="$1"
  shift
  [[ -n "$database_url" && "$#" -gt 0 ]] \
    || dr_die "database URL and command are required"
  dr_require_command python3
  python3 "$SCRIPT_DIR/libpq-exec.py" \
    --database-url-fd 3 --connect-timeout 10 -- "$@" 3<<< "$database_url"
}

dr_require_production_opt_in() {
  local environment="$1"
  local allow_production="$2"
  if [[ "$environment" == "production" && "$allow_production" != "true" ]]; then
    dr_die "production is refused by default; pass --allow-production under an approved change"
  fi
}

dr_require_confirmation() {
  local supplied="$1"
  local expected="$2"
  [[ "$supplied" == "$expected" ]] || dr_die "confirmation mismatch; use the exact documented operation token"
}

dr_safe_directory() {
  local directory="$1"
  [[ -n "$directory" && "$directory" != "/" && "$directory" != "$HOME" ]] || dr_die "unsafe directory"
  [[ ! -L "$directory" ]] || dr_die "directory must not be a symbolic link"
  mkdir -p -- "$directory"
  [[ -d "$directory" && -w "$directory" ]] || dr_die "directory is not writable"
  dr_require_command python3
  python3 - "$directory" <<'PY' || dr_die "directory must be owner-only, non-shared, and symlink-free"
import os,stat,sys
path=os.path.abspath(sys.argv[1])
if os.path.realpath(path)!=path:
    raise SystemExit(1)
value=os.lstat(path)
if not stat.S_ISDIR(value.st_mode) or value.st_uid!=os.getuid() or value.st_mode&0o077:
    raise SystemExit(1)
PY
}

dr_sha256() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$path" | awk '{print $1}'
  else
    dr_die "sha256sum or shasum is required"
  fi
}

dr_now() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

dr_timestamp() {
  date -u '+%Y%m%dT%H%M%SZ'
}

dr_file_size() {
  wc -c < "$1" | tr -d '[:space:]'
}

dr_write_json() {
  # JSON generation is centralized so shell values cannot break evidence files.
  dr_require_command python3
  python3 - "$@"
}
