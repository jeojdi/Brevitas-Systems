#!/usr/bin/env bash
# =============================================================================
#  migrate-project-data.sh
#
#  Copy ALL application row data from the Supabase project that currently serves
#  the live site (amjccgcgkcpbyevkjabw) INTO the declared production project
#  (wyfzmfnswtzyhwbltbpy), in foreign-key-safe order, with per-table row-count
#  reconciliation.
#
#  ---------------------------------------------------------------------------
#  !!  WARNING -- SEQUENCING HAZARD  -----------------------------------------
#  DO NOT repoint the deployed dashboard bundle (or the Railway API, or any
#  Supabase URL/anon/service-role env) to wyfzmfnswtzyhwbltbpy until THIS
#  migration has completed AND reconciled clean. The live bundle currently
#  reads amjccgcgkcpbyevkjabw. If you cut the bundle over first, live users
#  authenticate against an empty project and lose their accounts, keys, usage
#  and billing history. Order is: (1) run this migration, (2) reconcile,
#  (3) ONLY THEN repoint the bundle. See docs/DATA_MIGRATION_amjcc_to_wyfz.md.
#  ---------------------------------------------------------------------------
#
#  NO AUTOMATED AGENT HAS RUN THIS. It is an owner-run tool. Claude/agents wrote
#  the script; a human with both projects' DB credentials must execute it. It
#  defaults to --dry-run and refuses to mutate anything without --execute plus a
#  typed confirmation.
#
#  Usage:
#    SRC_DATABASE_URL=... DST_DATABASE_URL=... \
#      scripts/db/migrate-project-data.sh [--dry-run] [--execute]
#          [--include-ephemeral] [--include-auth]
#          [--truncate-dst] [--allow-nonempty]
#          [--only "t1 t2 ..."] [--reconcile-only]
#
#    SRC_DATABASE_URL  session-pooler / direct DSN for amjccgcgkcpbyevkjabw
#                      (source of truth -- the project the live site reads today)
#    DST_DATABASE_URL  session-pooler / direct DSN for wyfzmfnswtzyhwbltbpy
#                      (destination -- schema must already be migrated: run
#                       scripts/db/apply-migrations.sh against it FIRST)
#
#  Both DSNs MUST be the SESSION-mode pooler (port 5432) or the direct DB
#  connection, NOT the transaction pooler (port 6543): pg_dump, setval(), and
#  --disable-triggers (SET session_replication_role=replica) require a real
#  session and the `postgres` role.
#
#  Neither DSN is ever echoed.
# =============================================================================

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

DRY_RUN=1          # default: dry-run. --execute flips this off.
EXECUTE=0
INCLUDE_EPHEMERAL=0
INCLUDE_AUTH=0
TRUNCATE_DST=0
ALLOW_NONEMPTY=0
RECONCILE_ONLY=0
ONLY_TABLES=""

# --- Table sets, in FK-dependency (load) order --------------------------------
# Durable business/user data. auth.users is handled separately (see --include-auth
# and the runbook) because every profiles/billing/org row FKs to it and it cannot
# be trivially copied. Sequence-owning identity columns (e.g. usage_log.id,
# waitlist.id) are preserved by pg_dump --data-only (OVERRIDING SYSTEM VALUE +
# setval), which is required so billing_ledger.usage_log_id keeps matching.
#
# public.user_keys is DELIBERATELY EXCLUDED: 202607170001_enterprise_tenancy.sql
# drops it on the destination (raw creds superseded by KMS key_repositories). Its
# rows live only in the Step-2 backup. Do not add it here.
#
# bash 3.2 (macOS default) has no associative arrays; keep plain ordered lists.
DURABLE_TABLES="
public.profiles
public.api_keys
public.provider_config
public.usage_log
public.organizations
public.billing_events
public.legal_acceptances
public.waitlist
public.bvx_device_auth
public.billing_accounts
public.organization_members
public.customers
public.service_accounts
public.key_repositories
public.organization_invitations
public.billing_ledger
public.devices
public.installations
public.active_company_selections
public.bvx_device_consumption_receipts
public.data_subject_requests
public.legal_holds
public.legal_hold_actions
public.billing_recovery_audit
public.billing_checkout_reservations
public.stripe_webhook_events
public.audit_events
"

# Ephemeral / regenerable operational state. Off by default -- copying these is
# usually wrong (caches warm up again, queues drain, rate-limit windows reset,
# worker cursors re-seek). Enable only with a specific reason via --include-ephemeral.
EPHEMERAL_TABLES="
public.semantic_cache
public.ai_jobs
public.shared_endpoint_rate_limits
public.compliance_retention_runs
public.compliance_retention_worker_state
public.backup_deletion_tombstones
"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)            DRY_RUN=1; EXECUTE=0; shift ;;
        --execute)            EXECUTE=1; DRY_RUN=0; shift ;;
        --include-ephemeral)  INCLUDE_EPHEMERAL=1; shift ;;
        --include-auth)       INCLUDE_AUTH=1; shift ;;
        --truncate-dst)       TRUNCATE_DST=1; shift ;;
        --allow-nonempty)     ALLOW_NONEMPTY=1; shift ;;
        --reconcile-only)     RECONCILE_ONLY=1; shift ;;
        --only)               ONLY_TABLES="$2"; shift 2 ;;
        -h|--help)            sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# --- Refuse to run without both DSNs -----------------------------------------
: "${SRC_DATABASE_URL:=}"
: "${DST_DATABASE_URL:=}"
if [[ -z "$SRC_DATABASE_URL" ]]; then
    echo "error: SRC_DATABASE_URL is unset (source: amjccgcgkcpbyevkjabw)" >&2
    exit 2
fi
if [[ -z "$DST_DATABASE_URL" ]]; then
    echo "error: DST_DATABASE_URL is unset (destination: wyfzmfnswtzyhwbltbpy)" >&2
    exit 2
fi
if [[ "$SRC_DATABASE_URL" == "$DST_DATABASE_URL" ]]; then
    echo "error: SRC and DST DSNs are identical -- refusing to migrate a project onto itself" >&2
    exit 2
fi

for bin in pg_dump psql; do
    command -v "$bin" >/dev/null 2>&1 || { echo "error: $bin not found on PATH (need Postgres 16/17 client)" >&2; exit 2; }
done

cd "$ROOT"

# --- Build the working table list --------------------------------------------
TABLES=""
if [[ -n "$ONLY_TABLES" ]]; then
    for t in $ONLY_TABLES; do
        case "$t" in
            *.*) TABLES="$TABLES $t" ;;
            *)   TABLES="$TABLES public.$t" ;;
        esac
    done
else
    TABLES="$DURABLE_TABLES"
    [[ $INCLUDE_EPHEMERAL -eq 1 ]] && TABLES="$TABLES $EPHEMERAL_TABLES"
fi
# Normalize whitespace into a clean, order-preserving list.
TABLES="$(printf '%s\n' $TABLES)"

# --- Helpers ------------------------------------------------------------------
# Counts run read-only against either side. DSNs never appear in output.
src_count() { psql "$SRC_DATABASE_URL" -v ON_ERROR_STOP=1 -qtAX -c "select count(*) from $1;" 2>/dev/null; }
dst_count() { psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qtAX -c "select count(*) from $1;" 2>/dev/null; }
dst_exists() {
    psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qtAX \
      -c "select to_regclass('$1') is not null;" 2>/dev/null
}

banner() {
    echo "============================================================================="
    echo " migrate-project-data.sh   amjccgcgkcpbyevkjabw  -->  wyfzmfnswtzyhwbltbpy"
    echo "   mode: $([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo EXECUTE)"
    echo "   tables: $(printf '%s\n' $TABLES | grep -c .)  (ephemeral=$INCLUDE_EPHEMERAL auth=$INCLUDE_AUTH)"
    echo "============================================================================="
}

# --- Preflight: destination schema present, note current occupancy ------------
preflight() {
    local missing="" nonempty="" t reg n
    echo ">> preflight: checking destination schema + occupancy"
    for t in $TABLES; do
        reg="$(dst_exists "$t" || true)"
        if [[ "$reg" != "t" ]]; then
            missing="$missing $t"
            continue
        fi
        n="$(dst_count "$t" || echo '?')"
        if [[ "$n" != "0" && "$n" != "?" ]]; then
            nonempty="$nonempty $t($n)"
        fi
    done
    if [[ -n "$missing" ]]; then
        echo "error: destination is missing tables:$missing" >&2
        echo "       run scripts/db/apply-migrations.sh against DST_DATABASE_URL first." >&2
        exit 1
    fi
    if [[ -n "$nonempty" ]]; then
        echo "note: destination already has rows in:$nonempty"
        if [[ $DRY_RUN -eq 0 && $TRUNCATE_DST -eq 0 && $ALLOW_NONEMPTY -eq 0 ]]; then
            echo "error: destination tables are not empty. pg_dump plain INSERTs will" >&2
            echo "       collide on primary keys. Choose one:" >&2
            echo "         --truncate-dst    empty the target tables first (re-runnable), or" >&2
            echo "         --allow-nonempty  proceed anyway (only if you know they hold a" >&2
            echo "                           disjoint keyspace -- otherwise the load aborts)." >&2
            exit 1
        fi
    fi
}

# --- Optional truncate of destination (idempotent re-run support) -------------
truncate_dst() {
    [[ $TRUNCATE_DST -eq 1 ]] || return 0
    # One statement so mutual FKs truncate together; RESTART IDENTITY resets the
    # owned sequences so a later fresh insert does not collide with copied ids.
    local list
    list="$(printf '%s, ' $TABLES | sed 's/, $//')"
    echo ">> truncating destination tables (RESTART IDENTITY): $list"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "   [dry-run] would run: TRUNCATE $list RESTART IDENTITY;"
        return 0
    fi
    psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qX -c "truncate $list restart identity;"
}

# --- auth schema (optional, heavily guarded) ----------------------------------
# profiles/billing/org rows FK to auth.users. If the destination auth.users is
# NOT already populated (e.g. you migrated auth via the Supabase Auth admin API,
# or a prior project-linked auth copy), a --disable-triggers load will insert
# public rows whose user_id has no matching auth row -- consistent only until the
# next FK validation / RLS check. This path pg_dumps auth.users + auth.identities
# directly. It is imperfect (see runbook: encrypted columns, GoTrue-managed
# sequences) and OFF by default.
migrate_auth() {
    [[ $INCLUDE_AUTH -eq 1 ]] || { echo ">> skipping auth schema (default; see runbook / --include-auth)"; return 0; }
    echo ">> auth schema: copying auth.users, auth.identities (guarded)"
    local at src dst
    for at in auth.users auth.identities; do
        src="$(psql "$SRC_DATABASE_URL" -v ON_ERROR_STOP=1 -qtAX -c "select count(*) from $at;" 2>/dev/null || echo '?')"
        echo "   $at: source rows=$src"
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "   [dry-run] would: pg_dump --data-only --disable-triggers -t $at | psql DST"
            continue
        fi
        pg_dump "$SRC_DATABASE_URL" \
            --data-only --no-owner --no-privileges --disable-triggers \
            --column-inserts -t "$at" \
          | psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qX
        dst="$(psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qtAX -c "select count(*) from $at;" 2>/dev/null || echo '?')"
        echo "   $at: destination rows=$dst"
    done
}

# --- Per-table copy -----------------------------------------------------------
copy_table() {
    local t="$1" s
    s="$(src_count "$t" || echo '?')"
    if [[ "$s" == "0" ]]; then
        echo "   $t: source empty -- skip"
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "   [dry-run] $t: would copy $s rows"
        echo "             pg_dump --data-only --disable-triggers --column-inserts -t $t | psql DST"
        return 0
    fi
    echo "   $t: copying $s rows"
    # --disable-triggers defers FK + RLS during load (needs the postgres role on a
    # session connection). --column-inserts is verbose but resilient and keeps the
    # OVERRIDING SYSTEM VALUE form for identity columns. ON_ERROR_STOP aborts the
    # whole table load on the first bad row rather than half-loading.
    pg_dump "$SRC_DATABASE_URL" \
        --data-only --no-owner --no-privileges --disable-triggers \
        --column-inserts -t "$t" \
      | psql "$DST_DATABASE_URL" -v ON_ERROR_STOP=1 -qX
}

# --- Reconciliation -----------------------------------------------------------
reconcile() {
    local t s d mismatches=0
    echo ">> reconciliation (source count vs destination count)"
    printf "   %-42s %10s %10s   %s\n" "table" "src" "dst" "status"
    for t in $TABLES; do
        s="$(src_count "$t" || echo '?')"
        d="$(dst_count "$t" || echo '?')"
        if [[ "$s" == "$d" ]]; then
            printf "   %-42s %10s %10s   OK\n" "$t" "$s" "$d"
        else
            printf "   %-42s %10s %10s   MISMATCH\n" "$t" "$s" "$d"
            mismatches=$((mismatches + 1))
        fi
    done
    if [[ $mismatches -gt 0 ]]; then
        echo "RESULT: $mismatches table(s) do not reconcile. DO NOT repoint the bundle." >&2
        return 1
    fi
    echo "RESULT: all tables reconcile."
    return 0
}

# --- Confirmation gate --------------------------------------------------------
confirm_execute() {
    [[ $DRY_RUN -eq 1 ]] && return 0
    echo
    echo "About to WRITE live data into destination project wyfzmfnswtzyhwbltbpy."
    echo "This is a production data migration. Type exactly:  MIGRATE amjcc TO wyfz"
    printf "confirmation> "
    local reply=""
    read -r reply || true
    if [[ "$reply" != "MIGRATE amjcc TO wyfz" ]]; then
        echo "confirmation did not match -- aborting, nothing written." >&2
        exit 3
    fi
}

# ============================== main =========================================
banner

if [[ $RECONCILE_ONLY -eq 1 ]]; then
    reconcile
    exit $?
fi

preflight
confirm_execute
truncate_dst
migrate_auth

echo ">> copying tables in FK order"
for t in $TABLES; do
    copy_table "$t"
done

echo
reconcile
rc=$?

echo
if [[ $DRY_RUN -eq 1 ]]; then
    echo "dry-run complete. No data was written. Re-run with --execute to perform the copy."
else
    if [[ $rc -eq 0 ]]; then
        echo "migration complete AND reconciled."
        echo "NEXT: only now is it safe to repoint the dashboard bundle / API env to"
        echo "      wyfzmfnswtzyhwbltbpy. See docs/DATA_MIGRATION_amjcc_to_wyfz.md Step 7."
    else
        echo "migration finished with reconciliation MISMATCHES -- investigate before any cutover."
    fi
fi
exit $rc
