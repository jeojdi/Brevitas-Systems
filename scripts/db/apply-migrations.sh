#!/usr/bin/env bash
# Apply Supabase migrations in the order fixed by the release contract.
#
# The Supabase CLI derives apply order from the filesystem, which
# scripts/ci/migration-fresh-manifest.txt explicitly forbids ("never infer
# order from filesystem APIs"). Two filenames in this repo actually disagree
# between the two schemes -- 20260720_split_savings_metrics.sql sorts last
# lexically but parses as an early numeric version -- so `supabase db push`
# is not a safe applier here. This script reads the manifest instead.
#
# Applied migrations are recorded in public.brevitas_schema_migrations with a
# content checksum, so re-running is idempotent and body drift is fatal rather
# than silent.
#
# Usage:
#   scripts/db/apply-migrations.sh --db-url "$DB_URL" [--dry-run] [--status]
#                                  [--manifest scripts/ci/migration-fresh-manifest.txt]
#
# The connection string is never echoed.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="scripts/ci/migration-fresh-manifest.txt"
DB_URL=""
DRY_RUN=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db-url)   DB_URL="$2"; shift 2 ;;
        --manifest) MANIFEST="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --status)   STATUS_ONLY=1; shift ;;
        -h|--help)  sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$DB_URL" ]]; then
    DB_URL="${SUPABASE_DB_URL:-}"
fi
if [[ -z "$DB_URL" ]]; then
    echo "error: pass --db-url or set SUPABASE_DB_URL" >&2
    exit 2
fi

cd "$ROOT"
[[ -f "$MANIFEST" ]] || { echo "error: manifest not found: $MANIFEST" >&2; exit 2; }

psql_q() { psql "$DB_URL" -v ON_ERROR_STOP=1 -qtAX -c "$1"; }

LEDGER="public.brevitas_schema_migrations"

psql_q "create table if not exists ${LEDGER} (
            path        text primary key,
            sha256      text not null,
            applied_at  timestamptz not null default now()
        );" >/dev/null

# Manifest lines are paths; '#' lines are commentary.
# Built with a read loop rather than mapfile: macOS ships bash 3.2.
MIGRATIONS=()
while IFS= read -r line; do
    MIGRATIONS+=("$line")
done < <(grep -v '^[[:space:]]*#' "$MANIFEST" | grep -v '^[[:space:]]*$')
echo "manifest: ${MANIFEST} (${#MIGRATIONS[@]} migrations)"

pending=0
applied=0
skipped=0

for path in "${MIGRATIONS[@]}"; do
    [[ -f "$path" ]] || { echo "error: missing migration file: $path" >&2; exit 1; }
    sum="$(shasum -a 256 "$path" | awk '{print $1}')"
    recorded="$(psql_q "select sha256 from ${LEDGER} where path = '${path}';")"

    if [[ -n "$recorded" ]]; then
        if [[ "$recorded" != "$sum" ]]; then
            echo "FATAL drift: $path was applied with a different body" >&2
            echo "  recorded ${recorded}" >&2
            echo "  on disk  ${sum}" >&2
            exit 1
        fi
        skipped=$((skipped + 1))
        [[ $STATUS_ONLY -eq 1 ]] && echo "  applied  $path"
        continue
    fi

    pending=$((pending + 1))
    if [[ $STATUS_ONLY -eq 1 ]]; then
        echo "  PENDING  $path"
        continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  would apply  $path"
        continue
    fi

    echo "  applying  $path"
    # No appended suffix: `$(mktemp -t brevitas-migration).sql` names a *second*
    # path that mktemp never created, leaking the real temp file (and rm -f then
    # misses it). psql -f does not care about the extension, so use the file
    # mktemp actually created.
    tmp="$(mktemp -t brevitas-migration)"
    ledger_insert="insert into ${LEDGER} (path, sha256) values ('${path}', '${sum}')
                   on conflict (path) do update set sha256 = excluded.sha256;"

    # Migrations that open their own transaction cannot be nested; the ledger
    # write then lands immediately after their COMMIT. The rest are wrapped so
    # the schema change and its ledger row commit together.
    if grep -qiE '^[[:space:]]*begin[[:space:]]*;' "$path"; then
        { cat "$path"; printf '\n%s\n' "$ledger_insert"; } > "$tmp"
    else
        { printf 'begin;\n'; cat "$path"; printf '\n%s\ncommit;\n' "$ledger_insert"; } > "$tmp"
    fi

    if ! psql "$DB_URL" -v ON_ERROR_STOP=1 -qX -f "$tmp"; then
        rm -f "$tmp"
        echo "FAILED at $path -- database left at the last successful migration" >&2
        exit 1
    fi
    rm -f "$tmp"
    applied=$((applied + 1))
done

if [[ $STATUS_ONLY -eq 1 ]]; then
    echo "status: ${skipped} applied, ${pending} pending"
elif [[ $DRY_RUN -eq 1 ]]; then
    echo "dry run: ${skipped} already applied, ${pending} would be applied"
else
    echo "done: ${applied} applied, ${skipped} already present"
fi
