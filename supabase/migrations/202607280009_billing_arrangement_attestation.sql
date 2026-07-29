-- Phase 4a of the billing correctness rollout: the missing input behind
-- halting condition 2.
--
-- WHAT WAS WRONG
--
-- 202607280008 introduced halting condition 2 (zero-spend concentration) and
-- documented its purpose as protecting a PTU / committed-throughput /
-- Enterprise-agreement customer, "whose per-call marginal cost is zero because
-- the capacity is already paid for". Its runtime hint on the zero-spend branch
-- says the same thing in the imperative: 'PTU/committed-throughput/Enterprise
-- traffic is unbillable until attested'.
--
-- The predicate cannot do that. It classifies a row as having no marginal
-- dollar behind it with `coalesce(usage.actual_cost_usd, 0) <= 0`, but
-- usage_log.actual_cost_usd is not the customer's cost. It is computed by
-- brevitas/receipts.py calculate_costs() purely from model_price(provider,
-- model) — a static published-list-price table (MODEL_PRICES) keyed on
-- (provider, model) with NO organization dimension. There is no per-org price
-- override, contract price, or committed-capacity concept anywhere in this
-- schema or in the Python. So every ordinarily-priced call a PTU customer makes
-- carries a strictly positive list-price actual_cost_usd, and for such an org
--     zero_spend_rows = 0, zero_spend_net_savings_usd = 0, share = 0,
--     actual_spend_usd = large,
-- i.e. BOTH sub-tests pass and the org is invoiced 25% of list-price "savings"
-- against capacity it had already bought outright. That is precisely the
-- failure 202607280008 calls "the worst failure mode available to this system".
--
-- (Condition 2 does fire — on the population 202607280008 explicitly
-- exonerates. An exact_cache replay emits all four token legs zero
-- (brevitas/proxy.py _report_cache_hit), is priced with actual_cost_usd = 0,
-- and is authoritative + byte-preserving, so it carries positive verified
-- savings and a replay-heavy org halts. Halting under-bills, which is the safe
-- direction, so that half is deliberately left exactly as it is. But it means
-- condition 2's only live effect today is the inverse of its stated intent.)
--
-- WHY IT CANNOT BE PATCHED IN THE PREDICATE
--
-- Contract shape is not recoverable by any arithmetic over list-price columns.
-- It is metadata that simply does not exist in usage_log. This is a missing
-- INPUT, not a wrong formula. So this migration supplies the input that hint
-- already presumed exists: an explicit, per-organization billing-arrangement
-- attestation that DEFAULTS TO UNBILLABLE (absence of a row = unattested), and
-- a halting condition that refuses any positive fee for an organization not
-- attested as paying a real marginal per-call price.
--
-- HOW IT COMPOSES (read this before adding a caller)
--
-- 202607280008's assert_billing_period_halting_conditions is NOT modified here.
-- Its arithmetic conditions are correct and are actively being extended; this
-- migration adds the non-numeric condition around them instead of rewriting
-- them. The composition is enforced by privilege, not by convention:
--
--   * public.assert_billing_period_settlement_allowed is the ONLY entry point
--     the settlement path may call. It checks the attestation and then
--     delegates to the 202607280008 guard, returning its evidence unchanged
--     plus a billing_arrangement key.
--   * EXECUTE on public.assert_billing_period_halting_conditions is REVOKED
--     from service_role here. The inner guard is a complete arithmetic check
--     and an incomplete settlement check; nothing at runtime should be able to
--     reach it directly and thereby skip the attestation. Both functions are
--     SECURITY DEFINER, so the wrapper can still call it.
--
-- Fail-closed consequences, stated plainly: after this migration every
-- organization is unbillable until someone attests it. That is intentional. The
-- Phase 0 freeze (202607280006) is still in force and nothing calls either
-- function yet, so this changes no invoice today; it changes what "the halting
-- conditions are satisfied" is allowed to mean when the freeze is lifted.
--
-- TODO(billing-attestation-writer): there is deliberately NO application write
-- path for public.organization_billing_arrangement — service_role gets SELECT
-- only, so no runtime code (and no leaked service key) can attest an org into
-- billability. Populating it is an out-of-band, human, reviewed act (a
-- migration or a DBA psql session) and the value must be read off the actual
-- provider agreement. BLOCKER for whoever lifts the Phase 0 freeze: decide and
-- document who is entitled to attest and what evidence they must cite, then
-- build that process (not an RPC) before the first settlement. Until then
-- settlement halts for every org, which is the correct default.
--
-- TODO(condition-2-hint): the zero-spend hint inside
-- assert_billing_period_halting_conditions still reads
-- 'PTU/committed-throughput/Enterprise traffic is unbillable until attested'.
-- It is corrected in place in 202607280008 alongside this migration; if that
-- text ever returns, it is describing this migration's condition, not its own.
--
-- Safety: additive. One table, two functions, one revoke, and comment text. It
-- attaches nothing to any table, grants nothing to anon/authenticated, and
-- cannot cause a fee to be charged — it can only cause a settlement attempt to
-- abort.

begin;

do $halting_conditions_precondition$
begin
    if to_regclass('public.billing_halting_conditions') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280009 requires public.billing_halting_conditions from 202607280008',
            hint    = 'Apply 202607280008 first.';
    end if;
    if to_regprocedure(
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280009 requires public.assert_billing_period_halting_conditions '
                      'from 202607280008',
            hint    = 'This migration wraps that guard; it does not replace it.';
    end if;
end;
$halting_conditions_precondition$;

-- Same reason 202607280008 refused to install while the per-row trigger was
-- attached: a per-row fee path bypasses this attestation entirely.
do $freeze_still_held$
begin
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280009 refuses to install the billing-arrangement attestation '
                      'while the per-row fee trigger is attached',
            hint    = 'Reapply 202607280006 first.';
    end if;
end;
$freeze_still_held$;

-- Absence of a row is the unbillable state. Do not add a default row for an
-- organization anywhere, and do not backfill this table.
create table if not exists public.organization_billing_arrangement (
    organization_id uuid primary key
        references public.organizations(id) on delete cascade,
    arrangement text not null,
    attested_by text not null,
    attested_evidence text not null default '',
    attested_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Stated separately so a pre-existing table (create-if-not-exists is a no-op)
-- still acquires the bounds.
alter table public.organization_billing_arrangement
    drop constraint if exists organization_billing_arrangement_arrangement_check;
alter table public.organization_billing_arrangement
    add constraint organization_billing_arrangement_arrangement_check
    check (arrangement in (
        -- The org pays a real, per-call marginal price at or near list. Only
        -- this value permits a positive fee.
        'marginal_per_call',
        -- PTU / provisioned throughput / committed throughput / any prepaid or
        -- capacity-style agreement. The per-call marginal cost is not the list
        -- price, so a percentage of list-price savings is not billable.
        'committed_capacity',
        -- Looked at, could not be determined. Recorded explicitly so that
        -- "nobody checked" and "checked, unclear" stay distinguishable; both
        -- are unbillable.
        'unknown'
    ));

alter table public.organization_billing_arrangement
    drop constraint if exists organization_billing_arrangement_attested_by_check;
alter table public.organization_billing_arrangement
    add constraint organization_billing_arrangement_attested_by_check
    check (char_length(attested_by) between 1 and 200);

alter table public.organization_billing_arrangement enable row level security;
-- Service-owned and READ-ONLY even to service_role: attestation is an
-- out-of-band human act, never something running code can perform on itself.
revoke all on table public.organization_billing_arrangement
    from public, anon, authenticated, service_role;
grant select on table public.organization_billing_arrangement to service_role;

comment on table public.organization_billing_arrangement is
    'Per-organization billing-arrangement attestation. An organization with no '
    'row here is UNATTESTED and therefore unbillable; only arrangement = '
    '''marginal_per_call'' permits a positive settlement fee. This is contract '
    'metadata that cannot be derived from usage_log: usage_log.actual_cost_usd '
    'is published list price (brevitas/receipts.py MODEL_PRICES) and has no '
    'organization dimension, so a committed-capacity customer''s rows are '
    'indistinguishable from a pay-as-you-go customer''s. Deliberately not '
    'writable by service_role.';
comment on column public.organization_billing_arrangement.arrangement is
    'marginal_per_call (billable) | committed_capacity (PTU/provisioned/prepaid, '
    'not billable) | unknown (not billable).';
comment on column public.organization_billing_arrangement.attested_evidence is
    'Free text citing the provider agreement or invoice the attestation was read '
    'from. Not parsed; exists so an attestation can be audited later.';

create or replace function public.organization_billing_arrangement_state(
    p_organization_id uuid
)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    select coalesce(
        (
            select arrangement
              from public.organization_billing_arrangement
             where organization_id = p_organization_id
        ),
        'unattested'
    );
$$;

revoke all on function public.organization_billing_arrangement_state(uuid)
    from public, anon, authenticated;
grant execute on function public.organization_billing_arrangement_state(uuid)
    to service_role;

comment on function public.organization_billing_arrangement_state(uuid) is
    'Returns the attested billing arrangement for an organization, or '
    '''unattested'' when no attestation exists. Never returns null: the absence '
    'of an attestation is itself a state, and it is the unbillable one.';

-- The single settlement entry point. Attestation first, then the 202607280008
-- arithmetic conditions verbatim.
create or replace function public.assert_billing_period_settlement_allowed(
    p_organization_id uuid,
    p_period_start timestamptz,
    p_period_end timestamptz,
    p_fee_microusd bigint
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_arrangement text;
    v_evidence jsonb;
begin
    v_arrangement := public.organization_billing_arrangement_state(p_organization_id);

    -- Checked before the arithmetic conditions because it is not derivable from
    -- any of them: an org on committed capacity produces perfectly ordinary
    -- evidence, since actual_cost_usd is list price and carries no organization
    -- dimension. Settling zero is always allowed; it moves no money. Argument
    -- validation is left to the inner guard, so a malformed call still reports
    -- 22023 rather than being mistaken for an attestation problem.
    if p_organization_id is not null
       and p_fee_microusd is not null
       and p_fee_microusd > 0
       and v_arrangement <> 'marginal_per_call' then
        raise exception using
            errcode = '55000',
            message = format(
                'halting_condition=unattested_billing_arrangement: organization billing '
                'arrangement is %s, which is not billable',
                v_arrangement
            ),
            detail = format(
                'organization=%s period=[%s,%s) fee_microusd=%s arrangement=%s',
                p_organization_id, p_period_start, p_period_end,
                p_fee_microusd, v_arrangement
            ),
            hint = 'A percentage of list-price savings is billable only when the org '
                   'actually pays a marginal per-call price. PTU/committed-throughput/'
                   'prepaid capacity cannot be detected from usage_log at all — attest '
                   'the organization in public.organization_billing_arrangement from its '
                   'provider agreement, or do not settle this period.';
    end if;

    v_evidence := public.assert_billing_period_halting_conditions(
        p_organization_id, p_period_start, p_period_end, p_fee_microusd
    );

    return v_evidence || jsonb_build_object('billing_arrangement', v_arrangement);
end;
$$;

revoke all on function public.assert_billing_period_settlement_allowed(uuid, timestamptz, timestamptz, bigint)
    from public, anon, authenticated;
grant execute on function public.assert_billing_period_settlement_allowed(uuid, timestamptz, timestamptz, bigint)
    to service_role;

-- The inner guard becomes unreachable at runtime: it is a complete arithmetic
-- check and an incomplete settlement check, and the only way to guarantee the
-- attestation is evaluated is to make the wrapper the sole reachable door.
revoke execute on function public.assert_billing_period_halting_conditions(uuid, timestamptz, timestamptz, bigint)
    from public, anon, authenticated, service_role;

comment on function public.assert_billing_period_settlement_allowed(uuid, timestamptz, timestamptz, bigint) is
    'THE settlement guard. The period settlement path MUST call this — not '
    'assert_billing_period_halting_conditions, whose EXECUTE is revoked from '
    'service_role — in its own transaction before writing a settlement row, '
    'passing the fee in microUSD (floor(fee_usd * 1000000)). Enforces the '
    'billing-arrangement attestation (halting_condition='
    'unattested_billing_arrangement) and then every 202607280008 condition, '
    'returning that guard''s evidence plus billing_arrangement. Raises SQLSTATE '
    '55000 when settlement must not proceed.';

-- Correct the 202607280008 comments that presented condition 2 as PTU
-- protection. It never was; the attestation is.
comment on table public.billing_halting_conditions is
    'Phase 4 halting-condition thresholds. Single row. CHECK-bounded so the '
    'ceilings can be tightened operationally but never loosened without a '
    'migration. Read by public.assert_billing_period_halting_conditions, which '
    'is reached only through public.assert_billing_period_settlement_allowed '
    '(202607280009), where the non-numeric billing-arrangement attestation is '
    'enforced.';
comment on column public.billing_halting_conditions.max_zero_spend_savings_share is
    'Halting condition 2: maximum share of a period''s net savings that may come '
    'from rows with zero/absent actual_cost_usd. 0.50 caps the effective rate '
    'against provably-paid-for savings at 0.25/(1-0.50) = 50%. NOTE: this is a '
    'concentration test over RECORDED LIST-PRICE cost only. It does NOT detect '
    'PTU/committed-throughput/Enterprise agreements — those price normally at '
    'list in usage_log and produce a zero-spend share of 0. '
    'public.organization_billing_arrangement is what covers that case.';

do $billing_arrangement_contract$
declare
    v_org uuid;
    v_window_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_window_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_result jsonb;
    v_message text;
begin
    -- An organization nobody has attested is unbillable, and says so.
    if public.organization_billing_arrangement_state(gen_random_uuid()) <> 'unattested' then
        raise exception '202607280009 does not default an unknown organization to unattested';
    end if;

    insert into public.organizations (name)
    values ('202607280009 attestation contract probe')
    returning id into v_org;

    if public.organization_billing_arrangement_state(v_org) <> 'unattested' then
        raise exception '202607280009 does not default a real organization to unattested';
    end if;

    -- The attestation must halt a positive fee before any arithmetic condition,
    -- and must name itself so an operator can tell the conditions apart.
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_window_start, v_window_end, 1::bigint
        );
        raise exception '202607280009 settled a positive fee for an unattested organization';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception '202607280009 halted for the wrong reason: %', v_message;
            end if;
    end;

    -- A zero fee still passes, still returns the inner guard's evidence, and
    -- reports the arrangement it saw.
    v_result := public.assert_billing_period_settlement_allowed(
        v_org, v_window_start, v_window_end, 0::bigint
    );
    if v_result->>'billing_arrangement' <> 'unattested'
       or (v_result->>'fee_ceiling_microusd')::bigint <> 0
       or (v_result->>'eligible_rows')::bigint <> 0 then
        raise exception '202607280009 zero-fee evidence is wrong: %', v_result;
    end if;

    -- Malformed arguments must still be argument errors, not attestation errors.
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_window_end, v_window_start, 1::bigint
        );
        raise exception '202607280009 accepted an inverted settlement period';
    exception
        when sqlstate '22023' then null;
    end;

    -- Only marginal_per_call is billable; committed_capacity and unknown are not.
    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by)
    values (v_org, 'committed_capacity', '202607280009 contract');

    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_window_start, v_window_end, 1::bigint
        );
        raise exception '202607280009 settled a positive fee for a committed-capacity org';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception '202607280009 committed_capacity halted for the wrong reason: %',
                    v_message;
            end if;
    end;

    update public.organization_billing_arrangement
       set arrangement = 'unknown'
     where organization_id = v_org;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_window_start, v_window_end, 1::bigint
        );
        raise exception '202607280009 settled a positive fee for an unknown arrangement';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) = 0 then
                raise exception '202607280009 unknown arrangement halted for the wrong reason: %',
                    v_message;
            end if;
    end;

    -- Attesting an org does NOT make it billable on its own: every arithmetic
    -- condition still applies, and this org has no savings at all.
    update public.organization_billing_arrangement
       set arrangement = 'marginal_per_call'
     where organization_id = v_org;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_window_start, v_window_end, 1::bigint
        );
        raise exception '202607280009 attestation alone permitted a fee with no savings';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            if position('unattested_billing_arrangement' in v_message) <> 0 then
                raise exception '202607280009 did not delegate to the arithmetic conditions: %',
                    v_message;
            end if;
    end;

    v_result := public.assert_billing_period_settlement_allowed(
        v_org, v_window_start, v_window_end, 0::bigint
    );
    if v_result->>'billing_arrangement' <> 'marginal_per_call' then
        raise exception '202607280009 does not report the attested arrangement: %', v_result;
    end if;

    -- Unrecognised arrangements are not silently accepted as billable.
    begin
        update public.organization_billing_arrangement
           set arrangement = 'enterprise_handshake'
         where organization_id = v_org;
        raise exception '202607280009 allowed an unrecognised billing arrangement';
    exception
        when check_violation then null;
    end;

    delete from public.organization_billing_arrangement where organization_id = v_org;
    delete from public.organizations where id = v_org;

    -- No browser role may reach the attestation, no runtime role may write one,
    -- and no runtime role may reach the inner guard directly and skip it.
    if has_function_privilege('anon',
           'public.organization_billing_arrangement_state(uuid)', 'EXECUTE')
       or has_function_privilege('authenticated',
           'public.organization_billing_arrangement_state(uuid)', 'EXECUTE')
       or has_function_privilege('anon',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception '202607280009 exposed billing arrangement evidence to a browser role';
    end if;
    if has_table_privilege('anon', 'public.organization_billing_arrangement', 'SELECT')
       or has_table_privilege('authenticated', 'public.organization_billing_arrangement', 'SELECT') then
        raise exception '202607280009 exposed the attestation table to a browser role';
    end if;
    if has_table_privilege('service_role', 'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'UPDATE')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'DELETE') then
        raise exception '202607280009 let a runtime role attest an organization into billability';
    end if;
    if has_function_privilege('service_role',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception '202607280009 left the attestation-free inner guard reachable at runtime';
    end if;
    if not has_function_privilege('service_role',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception '202607280009 did not leave the settlement guard callable by service_role';
    end if;
end;
$billing_arrangement_contract$;

commit;
