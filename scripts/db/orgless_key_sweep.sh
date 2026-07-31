#!/usr/bin/env bash
# =============================================================================
# ORG-LESS API KEY SWEEP -- runbook
# =============================================================================
#
# WHAT THIS IS
#   43 of 102 rows in public.api_keys carry organization_id IS NULL. They are all
#   key_type='legacy', all unrevoked, all unexpired, and all carry the default
#   scope set, so no route-level scope check distinguishes them. Because they
#   present no organization, they authorize on the owner_id STRING alone in the
#   four usage RPCs -- the hole 202607280035_usage_read_tenant_scope.sql closes.
#
#   This sweep does three things and only three things:
#     01_summary  -- is the cohort closed, is the leak real, what would revoke cost
#     02_keys     -- one row per key, everything needed to decide that key's fate
#     03_emit     -- PRINTS the remediation SQL. Does not run it.
#
# WHAT THIS IS NOT
#   It is not a migration and not a fix. It never writes. Every file opens with
#     begin; set transaction read only;
#   which PostgreSQL enforces: a stray UPDATE in any of them aborts the whole
#   file with "cannot execute UPDATE in a read-only transaction" rather than
#   touching production. This script re-checks that prologue before sending a
#   file, so editing the prologue out breaks the runner instead of the database.
#
# HOW TO RUN
#   bash scripts/db/orgless_key_sweep.sh            # all three, formatted
#   bash scripts/db/orgless_key_sweep.sh summary
#   bash scripts/db/orgless_key_sweep.sh keys
#   bash scripts/db/orgless_key_sweep.sh emit       # prints ONLY the generated SQL
#   bash scripts/db/orgless_key_sweep.sh emit > /tmp/remediation.sql
#
#   Targets whatever `supabase link` points at (prod is wyfzmfnswtzyhwbltbpy).
#   Redirect stdout only: the Supabase CLI writes "Initialising login role..."
#   to stderr, so `> file` yields reviewable SQL and `2>&1 >file` does not.
#   `supabase db query -f` returns rows from the LAST statement only, which is
#   why each file is a single SELECT rather than a script of them.
#
#   Every count quoted in the comments below is a SNAPSHOT of wyfz taken
#   2026-07-31. The queries recompute all of it; if a comment and the output
#   disagree, the output is right and the comment is stale.
#
# -----------------------------------------------------------------------------
# BEFORE YOU RUN ANY EMITTED STATEMENT
# -----------------------------------------------------------------------------
# 0. Run PHASE_0_PREFLIGHT and compare it to the emission you are holding.
#    The emission is a photograph; keys keep writing. If the counts moved,
#    re-emit. Do not reconcile by hand.
#
# 1. PHASE_1_BACKFILL -- sets organization_id from organizations.legacy_owner_id.
#    Check first:
#      - legacy_owner_id is the target, NOT organization_members. Owner c7e2782f
#        is an ACTIVE member of two organizations; membership does not name a
#        single org, legacy_owner_id (UNIQUE, 0 duplicates) does. Section
#        D_TARGET prints both so you can see the divergence, and 02_keys prints
#        role/status rather than a boolean, because a DISABLED member whose
#        org-less key still reads is exactly the case worth catching.
#      - the target org is the owner's own: 01_summary section C_EXPOSURE shows
#        0 rows readable in any org other than the owner's own legacy org, so
#        this backfill narrows access, it never widens it.
#      - 25 of the 43 have NO organizations row for their owner. They get no
#        backfill statement. Do not create organizations for them -- inventing a
#        tenant is a business decision, not a cleanup.
#    Reversible: set organization_id back to NULL.
#    Does NOT touch usage_log. History stays organization_id IS NULL on purpose;
#    those rows are billing evidence (see 202607280036's erasure fence).
#
# 2. PHASE_2_RENAME_OPTIONAL -- cosmetic on this database, load-bearing on a
#    rebuilt one. This database's api_keys predates 202607170001 and never got
#    that file's table constraints, so it lacks
#    unique (organization_id, name, environment) that a FRESH schema declares.
#    After phase 1, twelve keys named 'bvx' would violate it. Skip these unless
#    you intend to rebuild; the new names are visible to the customer.
#
# 3. PHASE_3_REVOKE -- WHAT BREAKS IF YOU REVOKE A LIVE KEY
#      - The credential stops authenticating immediately. Any bvx CLI, proxy
#        caller or CI job still presenting it starts failing on the next request.
#      - Setting revoked_at fires trigger api_keys_provider_config_cleanup
#        (202607200008_provider_credential_cleanup.sql:104-107), whose function
#        DELETEs public.provider_config for that key_hash: the customer's
#        provider API key and model choice. That row is gone. Clearing
#        revoked_at afterwards does not bring it back -- restoring it means PITR
#        or asking the customer to re-enter a credential you cannot read.
#      - Usage stops being recorded for that caller, so a revoke during a
#        billing window silently truncates that owner's evidence.
#    Check first, per key, in the 02_keys output:
#      - usage_24h and usage_7d are 0
#      - provider_config_rows is 0
#      - owner_live_org_keys > 0, or you have accepted that this owner goes dark
#    Every emitted revoke re-asserts the first two conditions in its own WHERE
#    clause, so it fails closed. UPDATE 0 means a guard fired: STOP, re-run
#    01/02, re-emit. Never delete a guard to make a statement "work".
#
# 4. PHASE_4_HOLD -- emitted as comments, never as statements. As of 2026-07-31
#    this covers 6 keys: 1 live (c24d9a1151f5, ~2,500 usage rows in 24h, owner
#    has ZERO other live credentials -- revoking it is a customer outage with no
#    fallback), 2 recently active, and 3 orphans that have history but no
#    organizations row for their owner. An orphan is not a candidate for
#    "just create an org for it".
#
# 5. PHASE_5_VERIFY -- run it, and diff 01_summary sections A, D and E against
#    the pre-change run.
#
# ORDERING: phases 1 and 3 overlap on purpose. Backfill first so that a key
# which is later un-revoked comes back correctly attributed; for a key you
# revoke and never restore, phase 1 was merely harmless.
#
# APPLY DISCIPLINE: one phase at a time, inside an explicit transaction, read
# the rowcount, then COMMIT. Never pipe this script's output straight into psql.
#
# WHAT THIS SWEEP DOES NOT FIX
#   Backfilling organization_id narrows what an org-less key can reach, but the
#   authorization hole itself lives in the usage RPC predicate, and 01_summary
#   section C_EXPOSURE reports 202607280035 as NOT applied to this database.
#   Until that migration lands, a key with no organization still authorizes on
#   owner_id alone. Treat the sweep as exposure reduction, not as the fix.
#
# RE-VERIFYING THE READ-ONLY CLAIM YOURSELF
#   Build a scratch database, append `update public.api_keys set name='x';`
#   before the final `commit;` of any of the three files, and run it there:
#   PostgreSQL answers "cannot execute UPDATE in a read-only transaction" and
#   the whole file aborts. Do that on a scratch database, never on wyfz.
# =============================================================================
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

summary_sql="$here/orgless_key_sweep_01_summary.sql"
keys_sql="$here/orgless_key_sweep_02_keys.sql"
emit_sql="$here/orgless_key_sweep_03_emit.sql"

# Structural read-only assertion. Not a keyword blacklist -- the emit file's
# output legitimately contains the words update and revoke inside string
# literals. What is checked is that the file's first two statements are exactly
# the prologue that makes the server refuse writes for the rest of the session.
assert_read_only() {
    local f="$1"
    local prologue
    prologue="$(grep -v -e '^[[:space:]]*--' -e '^[[:space:]]*$' "$f" | head -2 | tr -d ' \t')"
    if [ "$prologue" != "$(printf 'begin;\nsettransactionreadonly;')" ]; then
        echo "REFUSING TO RUN $f: missing 'begin; set transaction read only;' prologue." >&2
        echo "That prologue is the only thing making this sweep incapable of writing." >&2
        exit 2
    fi
}

run_sql() {
    local f="$1" mode="$2"
    assert_read_only "$f"
    supabase db query --linked -f "$f" | python3 -c '
import json, sys
raw = sys.stdin.read()
start = raw.find("{")
if start < 0:
    sys.stderr.write(raw)
    sys.exit(1)
doc = json.loads(raw[start:])
rows = doc.get("rows")
if rows is None:
    sys.stderr.write(json.dumps(doc)[:4000] + "\n")
    sys.exit(1)
mode = sys.argv[1]
if mode == "emit":
    # Bare SQL, nothing else, so this can be redirected to a file and reviewed.
    for r in rows:
        print(r["sql"])
elif mode == "summary":
    for r in rows:
        print("{0:<12} {1}".format(r["section"], r["item"]))
        print("             => {0}".format(r["detail"]))
else:
    # Deliberate reading order: identity, then traffic, then target, then the
    # things a revoke destroys, then the call. JSON key order is alphabetical
    # and puts ai_jobs_rows first, which buries the decision.
    preferred = ["ord", "key_fp", "key_name", "owner_fp", "created", "last_used_at",
                 "usage_rows", "usage_7d", "usage_24h", "last_write",
                 "authoritative_rows", "rows_already_org",
                 "target_org", "target_org_name", "owner_membership",
                 "billing_owner_differs", "owner_live_org_keys",
                 "provider_config_rows", "ai_jobs_rows", "audit_actor_rows",
                 "device_auth_rows", "device_receipt_rows", "installation_rows",
                 "key_repo_rows", "key_id", "verdict"]
    present = list(rows[0].keys()) if rows else []
    cols = [c for c in preferred if c in present] + [c for c in present if c not in preferred]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
' "$mode"
}

cd "$repo"
case "${1:-all}" in
    summary) run_sql "$summary_sql" summary ;;
    keys)    run_sql "$keys_sql" keys ;;
    emit)    run_sql "$emit_sql" emit ;;
    all)
        echo "=== 1/3  COHORT, EXPOSURE, BLAST RADIUS ==================================="
        run_sql "$summary_sql" summary
        echo
        echo "=== 2/3  PER-KEY DECISION TABLE =========================================="
        run_sql "$keys_sql" keys
        echo
        echo "=== 3/3  EMITTED REMEDIATION (NOT EXECUTED) =============================="
        run_sql "$emit_sql" emit
        ;;
    *)
        echo "usage: $0 [all|summary|keys|emit]" >&2
        exit 64
        ;;
esac
