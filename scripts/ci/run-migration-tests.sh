#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo 'DATABASE_URL is required for the ephemeral migration test.' >&2
  exit 2
fi

node scripts/ci/verify-migrations.mjs
node scripts/ci/validate-migration-dsn.mjs

# The first database session is forced read-only. Parsing the URI is necessary
# but not sufficient: verify the server actually reached is loopback before DDL.
server_address="$({ PGOPTIONS='-c default_transaction_read_only=on' \
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --tuples-only --no-align \
    --command 'select pg_catalog.host(pg_catalog.inet_server_addr())'; } | tr -d '[:space:]')"
case "${server_address}" in
  127.0.0.1|::1) ;;
  *)
    echo 'Migration integration tests refuse a database server not reached over loopback.' >&2
    exit 2
    ;;
esac

fresh_migrations=()
while IFS= read -r migration; do
  fresh_migrations+=("${migration}")
done < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-fresh-manifest.txt
)
upgrade_migrations=()
while IFS= read -r migration; do
  upgrade_migrations+=("${migration}")
done < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' \
    scripts/ci/migration-upgrade-manifest.txt
)
# Sanity-guard the manifests without pinning an absolute count: the exact set,
# order, and checksums are enforced rigorously by verify-migrations.mjs, so an
# absolute count here only goes stale every time a migration is legitimately
# added. Require both manifests non-empty and fresh = baseline + upgrade
# (i.e. the upgrade chain is a suffix of the fresh chain, so fresh >= upgrade).
if [[ "${#fresh_migrations[@]}" -eq 0 || "${#upgrade_migrations[@]}" -eq 0 ]]; then
  echo 'Migration manifests must be non-empty (fresh and upgrade).' >&2
  exit 1
fi
if [[ "${#fresh_migrations[@]}" -lt "${#upgrade_migrations[@]}" ]]; then
  echo 'Fresh manifest must contain at least the upgrade chain (fresh = baseline + upgrade).' >&2
  exit 1
fi
baseline_count=$((${#fresh_migrations[@]} - ${#upgrade_migrations[@]}))

# The upgrade chain must be exactly the tail of the fresh chain. Comparing the
# WHOLE upgrade array against that tail (rather than hand-binding a fixed number
# of array indexes, which is how 202607280005-202607280009 came to be listed in
# both manifests yet never applied by the upgrade path) is what makes a newly
# added migration fail closed: it has to appear in both manifests at the same
# offset, and the driver loop below then applies every entry unconditionally.
for ((index = 0; index < ${#upgrade_migrations[@]}; index++)); do
  fresh_index=$((baseline_count + index))
  if [[ "${upgrade_migrations[${index}]}" != "${fresh_migrations[${fresh_index}]}" ]]; then
    echo "Upgrade manifest entry $((index + 1)) (${upgrade_migrations[${index}]}) is not the corresponding fresh-manifest tail entry (${fresh_migrations[${fresh_index}]}); the two manifests disagree." >&2
    exit 1
  fi
done
# Anchor both ends of the known production baseline so the suffix comparison
# above cannot be satisfied by a wholesale re-slice of both manifests.
if [[ "${fresh_migrations[$((baseline_count - 1))]}" != 'supabase/migrations/20260716_stripe_billing_rate_25pct.sql' \
   || "${upgrade_migrations[0]}" != 'supabase/migrations/202607170001_enterprise_tenancy.sql' ]]; then
  echo 'The production baseline must end at 20260716_stripe_billing_rate_25pct and the upgrade suffix must start at 202607170001.' >&2
  exit 1
fi

# Named handles for the migrations whose harness handling is special: an extra
# fixture, a hand-written pre-COMMIT rollback invariant, an out-of-band applier,
# or a later re-apply. They are resolved BY FILENAME, never by array index, so
# inserting a migration cannot silently re-point them, and a migration that
# disappears from the manifest fails the run instead of quietly losing its
# fixtures. Every handle below is also the `case` pattern used by the driver
# loop, so the two can never drift apart.
manifest_entry() {
  local wanted="$1" candidate
  for candidate in "${upgrade_migrations[@]}"; do
    if [[ "${candidate}" == "${wanted}" ]]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done
  echo "Upgrade manifest is missing ${wanted}, which the harness has special handling for." >&2
  return 1
}
manifest_index() {
  local wanted="$1" candidate_index
  for ((candidate_index = 0; candidate_index < ${#upgrade_migrations[@]}; candidate_index++)); do
    if [[ "${upgrade_migrations[${candidate_index}]}" == "${wanted}" ]]; then
      printf '%s' "${candidate_index}"
      return 0
    fi
  done
  echo "Upgrade manifest is missing ${wanted}." >&2
  return 1
}
fresh_manifest_index() {
  local wanted="$1" candidate_index
  for ((candidate_index = 0; candidate_index < ${#fresh_migrations[@]}; candidate_index++)); do
    if [[ "${fresh_migrations[${candidate_index}]}" == "${wanted}" ]]; then
      printf '%s' "${candidate_index}"
      return 0
    fi
  done
  echo "Fresh manifest is missing ${wanted}." >&2
  return 1
}

tenancy_migration="$(manifest_entry 'supabase/migrations/202607170001_enterprise_tenancy.sql')"
cache_security_migration="$(manifest_entry 'supabase/migrations/202607170002_cache_security.sql')"
billing_recovery_migration="$(manifest_entry 'supabase/migrations/202607170004_billing_recovery.sql')"
scaling_migration="$(manifest_entry 'supabase/migrations/202607170006_database_scaling.sql')"
device_migration="$(manifest_entry 'supabase/migrations/202607170010_device_delivery_idempotency.sql')"
membership_migration="$(manifest_entry 'supabase/migrations/202607170011_active_memberships.sql')"
receipt_migration="$(manifest_entry 'supabase/migrations/202607170012_receipt_accounting_alignment.sql')"
webhook_migration="$(manifest_entry 'supabase/migrations/202607200001_stripe_webhook_durability.sql')"
waitlist_migration="$(manifest_entry 'supabase/migrations/202607200002_waitlist_security.sql')"
billing_owner_migration="$(manifest_entry 'supabase/migrations/202607200003_billing_owner_attribution.sql')"
stripe_ordering_migration="$(manifest_entry 'supabase/migrations/202607200004_stripe_event_ordering.sql')"
initial_service_key_migration="$(manifest_entry 'supabase/migrations/202607200005_initial_service_key.sql')"
company_billing_migration="$(manifest_entry 'supabase/migrations/202607200006_company_billing_authorization.sql')"
billing_recovery_scope_migration="$(manifest_entry 'supabase/migrations/202607200007_billing_recovery_scope.sql')"
provider_cleanup_migration="$(manifest_entry 'supabase/migrations/202607200008_provider_credential_cleanup.sql')"
multitab_sessions_migration="$(manifest_entry 'supabase/migrations/202607200009_multitab_dashboard_sessions.sql')"
shared_limits_migration="$(manifest_entry 'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql')"
compliance_billing_isolation_migration="$(manifest_entry 'supabase/migrations/202607200011_compliance_billing_isolation.sql')"
webhook_lease_renewal_migration="$(manifest_entry 'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql')"
billing_control_limits_migration="$(manifest_entry 'supabase/migrations/202607200013_billing_control_rate_limits.sql')"
checkout_reservation_migration="$(manifest_entry 'supabase/migrations/202607200014_billing_checkout_session_reservations.sql')"
provider_outbound_migration="$(manifest_entry 'supabase/migrations/202607200015_provider_outbound_ambiguity.sql')"
durable_onboarding_migration="$(manifest_entry 'supabase/migrations/202607200016_durable_onboarding.sql')"
billing_customer_owner_migration="$(manifest_entry 'supabase/migrations/202607200017_billing_customer_owner_fencing.sql')"
workspace_experiences_migration="$(manifest_entry 'supabase/migrations/202607200018_workspace_experiences.sql')"
split_savings_migration="$(manifest_entry 'supabase/migrations/20260720_split_savings_metrics.sql')"
service_role_data_plane_migration="$(manifest_entry 'supabase/migrations/202607220001_service_role_data_plane.sql')"
supabase_advisor_hardening_migration="$(manifest_entry 'supabase/migrations/202607220002_supabase_advisor_hardening.sql')"
device_expiry_migration="$(manifest_entry 'supabase/migrations/202607270001_bvx_device_auth_expiry_index.sql')"
cache_warming_migration="$(manifest_entry 'supabase/migrations/202607280001_cache_warming.sql')"
multi_provider_warming_migration="$(manifest_entry 'supabase/migrations/202607280003_multi_provider_warming.sql')"
onboarding_evidence_migration="$(manifest_entry 'supabase/migrations/202607280004_onboarding_local_proxy_evidence.sql')"
installation_activation_migration="$(manifest_entry 'supabase/migrations/202607280005_installation_on_device_activation.sql')"
retire_fee_trigger_migration="$(manifest_entry 'supabase/migrations/202607280006_retire_per_row_fee_trigger.sql')"
period_settlement_migration="$(manifest_entry 'supabase/migrations/202607280007_period_settlement_ledger.sql')"
halting_conditions_migration="$(manifest_entry 'supabase/migrations/202607280008_billing_halting_conditions.sql')"
billing_attestation_migration="$(manifest_entry 'supabase/migrations/202607280009_billing_arrangement_attestation.sql')"
period_settlement_latch_migration="$(manifest_entry 'supabase/migrations/202607280010_period_settlement_send_latches.sql')"
unsent_release_migration="$(manifest_entry 'supabase/migrations/202607280011_billing_ledger_unsent_release.sql')"
evidence_warm_days_migration="$(manifest_entry 'supabase/migrations/202607280012_settlement_evidence_warm_days.sql')"
settlement_writer_migration="$(manifest_entry 'supabase/migrations/202607280013_period_settlement_writer.sql')"
browser_privilege_completion_migration="$(manifest_entry 'supabase/migrations/202607280024_browser_role_privilege_completion.sql')"
waitlist_network_budget_migration="$(manifest_entry 'supabase/migrations/202607280025_waitlist_network_budget.sql')"
usage_dedupe_authority_migration="$(manifest_entry 'supabase/migrations/202607280026_usage_log_authority_dedupe.sql')"

# Migrations at or after 202607170011 are applied twice to prove idempotence.
# Deriving the boundary from the manifest (instead of listing the earlier
# migrations) keeps a newly added migration in the double-apply set by default:
# a non-idempotent addition then fails the harness rather than slipping through.
idempotence_boundary_index="$(manifest_index "${membership_migration}")"

apply_migration() {
  local migration="$1"
  echo "Applying ${migration}"
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file "${migration}"
}

# FORWARD-ONLY SCHEMA CHANGE CONTRACT
#
# Read this before looking for a down-migration: there are none, and that is the
# declared posture, not an oversight.
#
#   * The schema is FORWARD-ONLY. A defect in an applied migration is corrected
#     by a NEW forward migration, never by editing or reversing the original.
#     supabase/migrations/*.sql are SHA-256 pinned in
#     scripts/ci/migration-frozen-checksums.txt, and the production project has
#     no migration ledger of its own, so an edited or un-applied file
#     desynchronizes the repository from reality with no way to detect it.
#   * The sanctioned SCHEMA rollback of last resort is PITR to the pre-cutover
#     checkpoint (docs/GO_LIVE_RUNBOOK.md, Phase 8), not a reverse script. For
#     every data-transforming migration in this chain that is the only honest
#     answer: an inverse of 202607270002_widen_billing_events_money would
#     truncate real money precision, and an inverse of the settlement-evidence
#     migrations would delete settlement evidence -- which
#     scripts/ci/verify-migrations.mjs refuses by design.
#   * Every migration added from the cutoff in verify-migrations.mjs onward must
#     therefore DECLARE its reverse posture in a `-- REVERSE:` header line, and
#     verifyReversePosture() fails the build if it does not. The declaration is
#     the deliverable; a reverse file is optional and only exists for the three
#     schema-only layers reversed below.
#
# This function is NOT a reverse-path test, and it is named accordingly. It
# rewrites the migration with `select 1/0;` injected immediately before COMMIT,
# requires psql to fail with 'division by zero', and then requires the supplied
# invariant to hold in a read-only session -- i.e. it proves the ATOMICITY OF A
# FAILED APPLY (no partial state committed), nothing about reversibility. The
# genuine down-then-up legs in this harness are the three further down: the
# encrypted-cache layer, api/migrations/004_database_scaling.rollback.sql, and
# the receipt-accounting validation layer, each pinned by
# assert_authoritative_counts on both sides.
assert_failed_apply_is_atomic() {
  local migration="$1"
  local rollback_query="$2"
  local failure_file failure_log rollback_state
  failure_file="$(mktemp)"
  failure_log="$(mktemp)"
  awk '
    {
      normalized=tolower($0)
      gsub(/[[:space:]]/, "", normalized)
      if (normalized == "commit;") {
        print "select 1/0;"
        injected=1
      }
      print
    }
    END { if (!injected) exit 42 }
  ' "${migration}" >"${failure_file}"
  if psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file "${failure_file}" >"${failure_log}" 2>&1; then
    echo "Failure-injected migration unexpectedly committed: ${migration}" >&2
    return 1
  fi
  if ! grep -q 'division by zero' "${failure_log}"; then
    echo "Failure-injected migration did not reach the pre-COMMIT fault: ${migration}" >&2
    sed -n '1,120p' "${failure_log}" >&2
    return 1
  fi
  rollback_state="$({ PGOPTIONS='-c default_transaction_read_only=on' \
    psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
      --tuples-only --no-align --command "select (${rollback_query})"; } | tr -d '[:space:]')"
  if [[ "${rollback_state}" != 't' ]]; then
    echo "Failure-injected migration left partial state: ${migration}" >&2
    return 1
  fi
  rm -f -- "${failure_file}" "${failure_log}"
}

bootstrap_database() {
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-bootstrap.sql
}

run_forward_assertions() {
  # Every file below runs with ON_ERROR_STOP=1. Without it psql exits 0 even
  # when a DO block raises, so a failing assertion printed ERROR into the log
  # and the harness still reported 'checks passed' -- verified by injecting a
  # deliberate 'raise exception' into one suite and watching the whole run exit
  # 0. Every assertion in this function was advisory until this line changed.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file scripts/ci/migration-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file scripts/ci/migration-cache-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file scripts/ci/migration-key-audit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file scripts/dr/compliance-workflow-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-device-membership-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-receipt-accounting-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-active-company-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-waitlist-security-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-initial-service-key-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-company-billing-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-billing-recovery-scope-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-provider-credential-cleanup-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-multitab-dashboard-session-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/waitlist-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/billing-recovery-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-compliance-billing-isolation-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/billing-control-shared-rate-limit-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-checkout-session-reservation-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-supabase-advisor-hardening-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-provider-outbound-ambiguity-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-durable-onboarding-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-onboarding-local-proxy-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-billing-customer-owner-fencing-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-cache-warming-assertions.sql
  # Live behaviour of the money path. Each of these files opens its own
  # transaction and ends in ROLLBACK, so they are rerunnable, they leave no
  # financial rows behind, and they are order-independent with respect to each
  # other and to every suite above. They seed public.billing_ledger with direct
  # INSERTs because 202607280006 retired queue_brevitas_fee_after_usage.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-billing-claim-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-billing-sweeps-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-billing-anchor-assertions.sql
  # Period settlement. Order matters here: the period-settlement structure file
  # must precede the halting-condition file (which drives the guard through a
  # settlement it promotes to 'reported'), and the writer file must come last
  # because it exercises settle_billing_period against both of them. These
  # three require 202607280007-202607280013 to be applied, which in turn
  # requires 202607280010-202607280013 to be registered in both manifests.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-period-settlement-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-halting-conditions-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-settlement-writer-assertions.sql
  # The outbound claim/send path (202607280029). Runs after the writer file
  # because it needs promoted settlements to exist as a concept, and it asserts
  # the ONE property the writer's own suite cannot: that a claim leaves the row
  # 'pending' so 202607280010's latch can never strand an abandoned claim.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-settlement-sender-assertions.sql
  # The attestation surface (202607280009 + 202607280030). Runs after the
  # halting-condition file because it drives the SAME settlement guard, and it
  # is the only place in this harness where a SUCCESSFUL attestation is
  # executed: doing so needs session_user = 'brevitas_attestor', which needs SET
  # SESSION AUTHORIZATION, which is superuser-only and therefore unavailable to
  # 202607280030's own apply-time DO block on a hosted project. The migration
  # proves every refusal; this file proves the operator path works at all, and
  # proves the body still refuses a service_role session even when the EXECUTE
  # grant is forcibly restored.
  #
  # REQUIRES 202607280030 to be registered in migration-fresh-manifest.txt and
  # migration-upgrade-manifest.txt. Until it is, this file fails in its own
  # section 0 with 'the attestation writer tables are missing', which is a
  # missing-registration message, not a regression.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-attestation-surface-assertions.sql
  # NOTE: the FIRST anchored fee basis (202607280028) and its assertion file
  # remain QUARANTINED in docs/quarantine/ -- their review found a blocker plus
  # three highs, including a path where a $0.0000000001 payment unlocked $125 of
  # billing. Neither file is registered or wired here and neither may be
  # restored: 202607280031 below replaces them, and the quarantined assertion
  # file asserts the existence-gate semantics that design deletes.
  #
  # The observed-price cache fee basis (202607280031), which is that
  # replacement. Runs after the halting-condition and writer files because it
  # drives the SAME guard and the SAME writer, and after the attestation surface
  # because every settlement it performs needs an attested organization. It
  # opens its own transaction and ends in ROLLBACK, so it is rerunnable and
  # leaves no settlement behind.
  #
  # REQUIRES 202607280031 to be registered in migration-fresh-manifest.txt and
  # migration-upgrade-manifest.txt. Until it is, this file fails in its own
  # section 0 with a missing-registration message, not a regression.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-anchored-savings-v2-assertions.sql
  # Schema and authorization repairs (202607280014-202607280023). Each file
  # opens its own transaction and ends in ROLLBACK, so they are rerunnable,
  # order-independent, and leave no fixture rows behind. The browser-role file
  # is the only place in the suite that switches to anon/authenticated and sets
  # request.jwt.*, so it is the only non-vacuous RLS and view-invoker coverage.
  # Must run before the browser-role file below: it is what proves the bootstrap
  # still installs Supabase's default anon/authenticated grants, i.e. that every
  # revoke assertion in this function can actually fail.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-browser-privilege-baseline-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-browser-role-isolation-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-warming-lease-retention-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-company-admin-evidence-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-durable-job-expiry-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-compliance-evidence-assertions.sql
  # 202607280025-202607280026. Both open their own transaction and end in
  # ROLLBACK, so they are rerunnable and order-independent. The network-budget
  # file is the only live coverage of the third waitlist window and of the
  # limiter's retention story; the dedupe file is the only place the widened
  # usage uniqueness is executed in both directions (same-authority repeat still
  # rejected, cross-authority pair now admitted).
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/waitlist-network-budget-assertions.sql
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-usage-dedupe-authority-assertions.sql
  # 202607280035: an org-less key may not read org-owned usage rows. This is the
  # ONLY place in scripts/ci that exercises the org-less branch of the four
  # usage read predicates -- migration-assertions.sql's existing "usage_page
  # crossed the tenant boundary" check passes a non-null p_organization_id and
  # an empty p_owner_id, so it only ever drove branch 1, which was never the
  # broken one. Opens its own transaction and ends in ROLLBACK.
  #
  # REQUIRES 202607280035 to be registered in migration-fresh-manifest.txt and
  # migration-upgrade-manifest.txt. Until it is, this file fails in its own
  # section 0 with a missing-registration message, not a regression.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-usage-tenant-scope-assertions.sql
  # 202607280036: no erasure or retention path may delete a usage_log row at or
  # below a non-void period_settlement_ledger watermark, and the preservation
  # invariant must abort when one is. This is the ONLY place in scripts/ci where
  # that property is exercised: migration-compliance-evidence-assertions.sql
  # fences and counts on billing_ledger, which has had no writer since
  # 202607280006, so every check in it compares 0 to 0 and passes no matter what
  # the erasure deletes. Section 5 of this file is a mutation test -- it strips
  # the fence out of the live prosrc and requires the invariant to fire -- so
  # the suite cannot go green on a fence that merely deletes nothing. Opens its
  # own transaction and ends in ROLLBACK.
  #
  # REQUIRES 202607280036 to be registered in migration-fresh-manifest.txt and
  # migration-upgrade-manifest.txt. Until it is, this file fails in its own
  # section 0 with a missing-registration message, not a regression.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-settlement-evidence-preservation-assertions.sql
  # 202607280032: no browser role may EXECUTE a SECURITY DEFINER function in
  # schema public. Deliberately LAST in this function, because unlike every
  # other file here it asserts a property of the WHOLE schema rather than of a
  # named object: it enumerates prosecdef over public and fails on any function
  # anon or authenticated can call. Running it after every other suite means it
  # also sees anything they left behind (they all end in ROLLBACK, so the answer
  # should be nothing -- if it is not, that is worth failing on).
  #
  # This is the invariant no other suite expresses. Every existing function-
  # privilege assertion in this harness is a hardcoded allowlist -- `proname =
  # <literal>`, `proname like 'company_admin_%'`, `proname in (...)` -- so a
  # function nobody enumerated, or a project-level ALTER DEFAULT PRIVILEGES
  # re-grant that hits all of them at once, was invisible to the entire suite.
  # That is how production reached 99 anon-executable SECURITY DEFINER
  # functions while a fresh database built from this repo had zero.
  #
  # REQUIRES 202607280032 to be registered in migration-fresh-manifest.txt and
  # migration-upgrade-manifest.txt. Until it is, this file fails in its own
  # section 0 with a missing-registration message, not a regression.
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --file scripts/ci/migration-browser-function-privilege-assertions.sql
}

# Pre-COMMIT rollback invariant for the migrations whose atomicity is probed by
# assert_failed_apply_is_atomic. Empty output means "no probe for this
# migration"; the driver loop applies it either way. The case patterns are the
# manifest-resolved handles, so a manifest rename cannot orphan an invariant.
upgrade_rollback_invariant() {
  case "$1" in
    "${webhook_migration}")
      printf '%s' "to_regprocedure('public.claim_stripe_webhook_event(text,text,uuid,integer)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='stripe_webhook_events' and column_name='status')"
      ;;
    "${waitlist_migration}")
      printf '%s' "to_regprocedure('public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)') is null"
      ;;
    "${billing_owner_migration}")
      printf '%s' "to_regprocedure('public.enforce_service_key_billing_owner()') is null"
      ;;
    "${billing_recovery_scope_migration}")
      printf '%s' "to_regprocedure('public.manually_resolve_billing_ledger_entry(bigint,text,text)') is not null and to_regprocedure('public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text)') is null and to_regclass('public.billing_recovery_audit') is null"
      ;;
    "${provider_cleanup_migration}")
      printf '%s' "to_regprocedure('public.purge_expired_provider_configs(integer)') is null and not exists (select 1 from pg_constraint where conrelid='public.provider_config'::regclass and conname='provider_config_key_hash_fkey')"
      ;;
    "${multitab_sessions_migration}")
      printf '%s' "position('rotated_count' in pg_get_functiondef(to_regprocedure('public.company_admin_create_dashboard_session_key(uuid,uuid,text,text,timestamptz,text)')))=0"
      ;;
    "${shared_limits_migration}")
      printf '%s' "to_regclass('public.shared_endpoint_rate_limits') is null and to_regprocedure('public.consume_billing_recovery_attempt(uuid,uuid)') is null and pg_get_function_result(to_regprocedure('public.submit_waitlist_signup(text,text,text,text,text,text,text,text,boolean)'))='boolean'"
      ;;
    "${compliance_billing_isolation_migration}")
      printf '%s' "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='billing_events' and column_name='organization_id') and to_regprocedure('public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)') is null and to_regprocedure('public.compliance_export_subject_pre_company_identity(uuid,uuid,text)') is null"
      ;;
    "${webhook_lease_renewal_migration}")
      printf '%s' "to_regprocedure('public.renew_stripe_webhook_event_lease(text,uuid,integer)') is null and position('lease_expires_at >' in pg_get_functiondef(to_regprocedure('public.mark_stripe_webhook_event_processed(text,uuid)')))=0"
      ;;
    "${billing_control_limits_migration}")
      printf '%s' "to_regprocedure('public.consume_billing_control_attempt(uuid,uuid,text)') is null"
      ;;
    "${checkout_reservation_migration}")
      printf '%s' "to_regclass('public.billing_checkout_reservations') is null and to_regprocedure('public.reserve_billing_checkout_generation(uuid,text,uuid,integer)') is null and to_regprocedure('public.persist_billing_checkout_session(uuid,bigint,uuid,text)') is null"
      ;;
    "${provider_outbound_migration}")
      printf '%s' "to_regprocedure('public.mark_ai_job_provider_outbound_started(uuid,text)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='ai_jobs' and column_name='provider_outbound_started_at')"
      ;;
    "${durable_onboarding_migration}")
      printf '%s' "to_regprocedure('public.organization_onboarding_status(uuid,uuid)') is null and to_regprocedure('public.register_bvx_installation(uuid,text,uuid,text,text,text,text,text,text,text,text)') is null and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='organizations' and column_name='onboarding_completed_at') and not exists (select 1 from information_schema.columns where table_schema='public' and table_name='installations' and column_name='registration_key_hash')"
      ;;
    "${billing_customer_owner_migration}")
      printf '%s' "to_regprocedure('public.save_billing_customer_identity(uuid,text)') is null"
      ;;
    "${workspace_experiences_migration}")
      printf '%s' "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='organizations' and column_name='account_type') and to_regprocedure('public.ensure_workspace_organization(uuid,text,text)') is null"
      ;;
    "${split_savings_migration}")
      printf '%s' "not exists (select 1 from information_schema.columns where table_schema='public' and table_name='usage_log' and column_name='provider_input_tokens_avoided')"
      ;;
    "${service_role_data_plane_migration}")
      # This invariant used to read "not has_table_privilege(service_role,
      # organizations, SELECT)", which was vacuous: migration-bootstrap.sql did
      # not reproduce Supabase's default GRANT ALL, so service_role held nothing
      # on any table until this migration granted it. Now that the bootstrap
      # installs the production privilege baseline, state the invariant in the
      # direction that actually distinguishes the two schemas: rolled back, the
      # project defaults are still intact, so service_role retains privileges
      # this migration revokes and never re-grants (INSERT/DELETE on
      # organizations, INSERT/UPDATE/DELETE on organization_members).
      printf '%s' "has_table_privilege('service_role','public.organizations','INSERT') and has_table_privilege('service_role','public.organizations','DELETE') and has_table_privilege('service_role','public.organization_members','UPDATE')"
      ;;
    "${supabase_advisor_hardening_migration}")
      printf '%s' "(select proconfig is null from pg_proc where oid='public.company_role_permissions(text)'::regprocedure) and has_function_privilege('anon','public.handle_new_user()','EXECUTE')"
      ;;
    "${cache_warming_migration}")
      printf '%s' "to_regclass('public.warm_credentials') is null and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer)') is null"
      ;;
    "${multi_provider_warming_migration}")
      printf '%s' "to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer)') is not null and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb)') is null"
      ;;
    "${onboarding_evidence_migration}")
      printf '%s' "position('usage.authoritative is true' in pg_get_functiondef(to_regprocedure('public.organization_onboarding_status(uuid,uuid)'))) > 0 and position('usage.authoritative is true' in pg_get_functiondef(to_regprocedure('public.complete_organization_onboarding(uuid,uuid,text)'))) > 0"
      ;;
    "${retire_fee_trigger_migration}")
      # A rolled-back retirement must leave the per-row trigger attached: the
      # drop is the entire money-relevant effect of 202607280006.
      printf '%s' "exists (select 1 from pg_trigger trigger_state where trigger_state.tgrelid='public.usage_log'::regclass and trigger_state.tgname='queue_brevitas_fee_after_usage' and not trigger_state.tgisinternal)"
      ;;
    "${period_settlement_migration}")
      printf '%s' "to_regclass('public.period_settlement_ledger') is null and to_regprocedure('public.prevent_period_settlement_identity_change()') is null and to_regprocedure('public.period_settlement_fee_microusd(bigint,bigint)') is null"
      ;;
    "${halting_conditions_migration}")
      printf '%s' "to_regclass('public.billing_halting_conditions') is null and to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)') is null and to_regprocedure('public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)') is null"
      ;;
    "${billing_attestation_migration}")
      printf '%s' "to_regclass('public.organization_billing_arrangement') is null and to_regprocedure('public.organization_billing_arrangement_state(uuid)') is null and to_regprocedure('public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)') is null"
      ;;
    "${period_settlement_latch_migration}")
      # A rolled-back latch migration must leave the identity guard WITHOUT the
      # one-way send latches: that text is the whole money-relevant effect of
      # 202607280010, and a half-applied guard would let a reported period be
      # voided with its send evidence erased and then re-billed at full rate.
      printf '%s' "not exists (select 1 from pg_proc trigger_function where trigger_function.oid=to_regprocedure('public.prevent_period_settlement_identity_change()') and trigger_function.prosrc like '%old.outbound_started_at is not null%')"
      ;;
    "${unsent_release_migration}")
      printf '%s' "to_regprocedure('public.release_billing_ledger_unsent(bigint,text)') is null"
      ;;
    "${evidence_warm_days_migration}")
      # Rolled back, the evidence function must still be 202607280008's version,
      # i.e. counting (provider, day) rows rather than distinct days.
      printf '%s' "not exists (select 1 from pg_proc evidence_function where evidence_function.oid=to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)') and evidence_function.prosrc like '%count(distinct warm.day)%')"
      ;;
    "${settlement_writer_migration}")
      # The writer DROPs and recreates the evidence function to widen its OUT
      # list, so a rolled-back apply must leave BOTH the un-widened evidence
      # function (still present, without the watermark) and no writer, promoter,
      # summary, or sweep function behind.
      printf '%s' "to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)') is not null and position('usage_log_watermark_id' in pg_get_function_result(to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')))=0 and to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)') is null and to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)') is null and to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)') is null and to_regprocedure('public.billing_periods_awaiting_settlement(integer)') is null"
      ;;
    "${browser_privilege_completion_migration}")
      # 202607280024 is a privilege migration, so a half-applied one is the
      # dangerous shape: the widened assertion function committed while the four
      # REVOKEs did not would make the contract report a safety it never
      # established. Rolled back, both halves must still be at their 202607280015
      # state -- the browser roles keep Supabase's project-default privileges on
      # audit_events/organization_invitations (which the bootstrap installs and
      # migration-browser-privilege-baseline-assertions.sql proves are really
      # there, so this is not vacuous), and the contract function has not yet
      # heard of audit_events.
      printf '%s' "has_table_privilege('anon','public.audit_events','TRUNCATE') and has_table_privilege('authenticated','public.organization_invitations','SELECT') and position('audit_events' in pg_get_functiondef(to_regprocedure('public.assert_browser_role_table_privileges()')))=0"
      ;;
    "${waitlist_network_budget_migration}")
      printf '%s' "to_regprocedure('public.consume_waitlist_network_budget(text,integer,integer)') is null and to_regprocedure('public.purge_expired_shared_endpoint_rate_limits(integer)') is null"
      ;;
    "${usage_dedupe_authority_migration}")
      # The index swap must be all-or-nothing. A rolled-back apply must leave the
      # 20260710 index in place: an instant with NEITHER index is an instant in
      # which a billable receipt can be written twice.
      printf '%s' "to_regclass('public.usage_log_request_authority_unique') is null and to_regclass('public.usage_log_request_unique') is not null"
      ;;
    *)
      :
      ;;
  esac
}

applied_upgrade_migrations=()
record_upgrade_applied() {
  applied_upgrade_migrations+=("$1")
}

billing_migration_expected_host="$(node --input-type=module -e \
  "import { validateMigrationDsn } from './scripts/ci/validate-migration-dsn.mjs'; console.log(validateMigrationDsn(process.env.DATABASE_URL).hostname)")"
billing_migration_expected_database="$(node --input-type=module -e \
  "import { validateMigrationDsn } from './scripts/ci/validate-migration-dsn.mjs'; console.log(validateMigrationDsn(process.env.DATABASE_URL).database)")"
billing_version_fixture="${PWD}/scripts/ci/billing-maintenance-version-fetch-fixture.mjs"
run_billing_identity_maintenance() {
  NODE_OPTIONS="--import=${billing_version_fixture}" \
  BREVITAS_BILLING_ENABLED=false \
  BREVITAS_BILLING_MIGRATION_PHASE=api-worker-quiesced \
  BREVITAS_BILLING_MAINTENANCE_SHA=0000000000000000000000000000000000000000 \
  BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL=https://dashboard.example.invalid/api/version \
  BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL=http://127.0.0.1:43119/version \
  BREVITAS_BILLING_MAINTENANCE_OFFLINE_LOOPBACK_TEST=true \
  BREVITAS_BILLING_MIGRATION_EXPECTED_HOST="${billing_migration_expected_host}" \
  BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE="${billing_migration_expected_database}" \
    bash scripts/ci/apply-billing-identity-migrations.sh
}
billing_identity_complete_query="to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)') is not null and to_regprocedure('public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)') is not null and to_regprocedure('public.company_billing_authorize_actor(uuid)') is not null and (select procedure_state.proargnames[1] from pg_proc procedure_state where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_subscription_snapshot(uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz)'))='p_organization_id' and (select procedure_state.proargnames[1] from pg_proc procedure_state where procedure_state.oid=to_regprocedure('public.compare_and_set_stripe_invoice_snapshot(uuid,bigint,bigint,text,text,text,text)'))='p_organization_id' and exists (select 1 from pg_constraint constraint_state where constraint_state.conrelid='public.billing_accounts'::regclass and constraint_state.contype='p' and pg_get_constraintdef(constraint_state.oid)='PRIMARY KEY (organization_id)')"

echo 'Testing the known production-baseline upgrade through migration 017.'
bootstrap_database
for ((index = 0; index < baseline_count; index++)); do
  apply_migration "${fresh_migrations[${index}]}"
done
apply_migration scripts/ci/migration-upgrade-baseline-fixture.sql

prerequisite_log="$(mktemp)"
if psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file supabase/migrations/202607170006_database_scaling.sql \
  >"${prerequisite_log}" 2>&1; then
  echo 'Database-scaling migration unexpectedly succeeded without enterprise tenancy.' >&2
  exit 1
fi
if ! grep -q 'requires Supabase migrations through 202607170003 first' "${prerequisite_log}"; then
  echo 'Database-scaling prerequisite failure did not report its ordering contract.' >&2
  sed -n '1,120p' "${prerequisite_log}" >&2
  exit 1
fi

# Drive the ENTIRE upgrade manifest. Every entry is applied here; the case arms
# only add work (a banner, a staged index build, a fixture, a rollback probe, an
# out-of-band applier). Nothing is skipped by omission, which is the whole point:
# the previous hand-bound index list stopped at 202607280004 and left the Phase
# 0-4 billing schema (202607280005-202607280009) unapplied on this path.
for ((index = 0; index < ${#upgrade_migrations[@]}; index++)); do
  migration="${upgrade_migrations[${index}]}"

  case "${migration}" in
    "${membership_migration}")
      echo 'Applying migrations 011-013 twice to prove idempotence.'
      ;;
    "${webhook_migration}")
      echo 'Applying migrations 202607200001-200002 twice to prove idempotence.'
      ;;
    "${billing_owner_migration}")
      echo 'Applying migrations 202607200003 through the complete release suffix in order.'
      ;;
    "${device_expiry_migration}")
      echo 'Applying the 20260727-20260728 release tail twice to prove idempotence.'
      ;;
    "${installation_activation_migration}")
      echo 'Applying the 202607280005-202607280009 billing-correctness suffix (retires the per-row fee trigger).'
      ;;
  esac

  case "${migration}" in
    "${billing_recovery_migration}")
      echo 'Pre-staging upgrade indexes outside a transaction before migration 006.'
      apply_migration api/migrations/004_database_scaling.concurrent_indexes.sql
      ;;
  esac

  upgrade_probe="$(upgrade_rollback_invariant "${migration}")"
  if [[ -n "${upgrade_probe}" ]]; then
    assert_failed_apply_is_atomic "${migration}" "${upgrade_probe}"
  fi

  case "${migration}" in
    "${stripe_ordering_migration}")
      # 200004-200006 are applied only through the guarded maintenance procedure,
      # which owns their quiesce/version preconditions.
      echo 'Applying the guarded 200004-200006 billing maintenance procedure immediately after 200003.'
      run_billing_identity_maintenance
      echo 'Rerunning the guarded procedure to prove completed company-scoped state is validated and skipped.'
      run_billing_identity_maintenance
      record_upgrade_applied "${stripe_ordering_migration}"
      record_upgrade_applied "${initial_service_key_migration}"
      record_upgrade_applied "${company_billing_migration}"
      ;;
    "${initial_service_key_migration}"|"${company_billing_migration}")
      :
      ;;
    *)
      apply_migration "${migration}"
      record_upgrade_applied "${migration}"
      if ((index >= idempotence_boundary_index)); then
        apply_migration "${migration}"
      fi
      ;;
  esac

  case "${migration}" in
    "${tenancy_migration}")
      apply_migration scripts/ci/migration-cache-legacy-fixture.sql
      echo 'Testing the retired API cache compatibility guard outside the forward manifests.'
      apply_migration api/migrations/002_semantic_cache.sql
      psql "${DATABASE_URL}" --no-psqlrc \
        --file scripts/ci/migration-cache-guard-assertions.sql
      ;;
    "${device_migration}")
      apply_migration scripts/ci/migration-device-null-upgrade-fixture.sql
      echo 'Reapplying migration 010 to erase pre-constraint quarantined ciphertext.'
      apply_migration "${device_migration}"
      psql "${DATABASE_URL}" --no-psqlrc \
        --file scripts/ci/migration-device-null-upgrade-assertions.sql
      ;;
    "${company_billing_migration}")
      echo 'Failure-injecting reruns of 200004-200006 and requiring the completed company identity to roll back intact.'
      assert_failed_apply_is_atomic "${stripe_ordering_migration}" \
        "${billing_identity_complete_query}"
      assert_failed_apply_is_atomic "${initial_service_key_migration}" \
        "${billing_identity_complete_query}"
      assert_failed_apply_is_atomic "${company_billing_migration}" \
        "${billing_identity_complete_query}"
      ;;
  esac
done

# Fail closed rather than open: a manifest entry the loop above never applied is
# a harness defect, not a migration to be quietly skipped.
for ((index = 0; index < ${#upgrade_migrations[@]}; index++)); do
  upgrade_applied=0
  for ((applied_index = 0; applied_index < ${#applied_upgrade_migrations[@]}; applied_index++)); do
    if [[ "${applied_upgrade_migrations[${applied_index}]}" == "${upgrade_migrations[${index}]}" ]]; then
      upgrade_applied=1
      break
    fi
  done
  if [[ "${upgrade_applied}" -ne 1 ]]; then
    echo "Upgrade path never applied ${upgrade_migrations[${index}]}." >&2
    exit 1
  fi
done

psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-upgrade-assertions.sql
run_forward_assertions

echo 'Racing shared billing-recovery attempts from independent PostgreSQL sessions.'
bash scripts/ci/run-billing-recovery-shared-limit-test.sh

echo 'Racing shared Checkout attempts from independent PostgreSQL sessions.'
bash scripts/ci/run-billing-control-shared-limit-test.sh

echo 'Racing billing-owner transfer against customer persistence.'
bash scripts/ci/run-billing-owner-transfer-race-test.sh

echo 'Racing bounded cache writes from independent PostgreSQL sessions.'
cache_pids=()
for cache_index in 101 102 103 104 105 106 107 108; do
  psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
    --set cache_index="${cache_index}" \
    --file scripts/ci/migration-cache-concurrent-write.sql &
  cache_pids+=("$!")
done
for cache_pid in "${cache_pids[@]}"; do
  wait "${cache_pid}"
done
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-concurrency-assertions.sql

cache_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
echo 'Rolling back and reapplying the encrypted cache RPC/constraint layer.'
apply_migration scripts/ci/migration-cache-rollback.sql
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-rollback-assertions.sql
cache_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
if [[ "${cache_after}" != "${cache_before}" ]]; then
  echo 'Cache rollback changed encrypted cache row count.' >&2
  exit 1
fi
apply_migration "${cache_security_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-reapply-assertions.sql
cache_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.semantic_cache')"
if [[ "${cache_after}" != "${cache_before}" ]]; then
  echo 'Cache reapply changed encrypted cache row count.' >&2
  exit 1
fi

usage_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.usage_log')"
billing_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.billing_ledger')"
audit_before="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.audit_events')"

assert_authoritative_counts() {
  local stage="$1"
  local usage_after billing_after audit_after
  usage_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.usage_log')"
  billing_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.billing_ledger')"
  audit_after="$(psql "${DATABASE_URL}" --no-psqlrc --tuples-only --no-align --command 'select count(*) from public.audit_events')"
  if [[ "${usage_after}" != "${usage_before}" || "${billing_after}" != "${billing_before}" || "${audit_after}" != "${audit_before}" ]]; then
    echo "Database-scaling ${stage} changed authoritative row counts." >&2
    exit 1
  fi
}

echo 'Rolling back only the database-scaling read path.'
apply_migration api/migrations/004_database_scaling.rollback.sql
psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-rollback-assertions.sql
assert_authoritative_counts rollback

echo 'Reapplying the generated database-scaling migration.'
apply_migration "${scaling_migration}"
psql "${DATABASE_URL}" --no-psqlrc --file scripts/ci/migration-reapply-assertions.sql
assert_authoritative_counts reapply

echo 'Rolling back only the receipt-accounting validation layer.'
apply_migration scripts/ci/migration-receipt-accounting-rollback.sql
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-receipt-accounting-rollback-assertions.sql
assert_authoritative_counts receipt-rollback
echo 'Reapplying the receipt-accounting alignment migration across the retired trigger.'
# 202607170012 refuses to apply unless queue_brevitas_fee_after_usage is attached
# (202607170012:5-25) and 202607280006 has already retired it, so the frozen
# migration cannot be replayed as-is at this point in the chain. 202607280006
# deliberately RETAINS public.queue_brevitas_fee() (:18-21) precisely so that a
# re-attach is one statement; re-attach it for the duration of the replay only,
# then re-run 202607280006 to retire it again and assert it is gone. Nothing
# inserts into public.usage_log between the two, so no fee can be queued -- which
# assert_authoritative_counts below re-proves against the pre-rollback counts.
# 202607170012 itself is frozen (checksum-pinned, and already applied to a
# production database with no migration ledger) and is not edited here.
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --command \
  'create trigger queue_brevitas_fee_after_usage after insert on public.usage_log for each row execute function public.queue_brevitas_fee();'
apply_migration "${receipt_migration}"
apply_migration "${retire_fee_trigger_migration}"
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-receipt-accounting-assertions.sql
assert_authoritative_counts receipt-reapply

echo 'Resetting the loopback-only database for an isolated fresh install.'
psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 \
  --file scripts/ci/migration-reset-database.sql
bootstrap_database

echo 'Testing the complete fresh Supabase migration chain.'
# The fresh chain is applied in two segments with the frozen 010-013 +
# 202607200001-202607220002 replay in between, rather than replaying it into a
# fully-applied schema. Two reasons, both ordering:
#   1. 202607170012 raises unless queue_brevitas_fee_after_usage is attached
#      (202607170012:5-25), and 202607280006 retires it. Replaying after the full
#      chain therefore failed with '202607170012 requires the canonical usage and
#      billing trigger chain'.
#   2. The frozen replay clobbers later definitions: 202607200016 overwrites the
#      202607280004 onboarding functions, and 202607170010 overwrites the
#      202607280005 body of consume_bvx_device_idempotent. Applying the
#      202607270001+ tail after the replay restores both by construction, instead
#      of hand-patching 202607280004 back afterwards.
fresh_replay_start="$(fresh_manifest_index 'supabase/migrations/202607170010_device_delivery_idempotency.sql')"
fresh_replay_end="$(fresh_manifest_index 'supabase/migrations/202607220002_supabase_advisor_hardening.sql')"
if ((fresh_replay_start >= fresh_replay_end || fresh_replay_end + 1 >= ${#fresh_migrations[@]})); then
  echo 'Fresh manifest does not order the frozen replay window before a remaining tail.' >&2
  exit 1
fi
for ((index = 0; index <= fresh_replay_end; index++)); do
  apply_migration "${fresh_migrations[${index}]}"
done
echo 'Reapplying frozen migrations 010-013 and the 202607200001-202607220002 suffix on the isolated fresh install.'
for ((index = fresh_replay_start; index <= fresh_replay_end; index++)); do
  apply_migration "${fresh_migrations[${index}]}"
done
echo 'Applying the remaining fresh chain (202607270001 onward) after the frozen replay.'
for ((index = fresh_replay_end + 1; index < ${#fresh_migrations[@]}; index++)); do
  apply_migration "${fresh_migrations[${index}]}"
done
psql "${DATABASE_URL}" --no-psqlrc \
  --file scripts/ci/migration-cache-fresh-assertions.sql
run_forward_assertions

echo 'Ephemeral fresh-install and production-upgrade migration checks passed.'
