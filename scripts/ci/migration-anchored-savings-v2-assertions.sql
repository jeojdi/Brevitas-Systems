\set ON_ERROR_STOP on

-- Behavioural contract for the OBSERVED-PRICE CACHE FEE BASIS
-- (supabase/migrations/202607280031_observed_price_cache_fee_basis.sql).
--
-- THIS IS THE SECOND ATTEMPT. The first (202607280028) is QUARANTINED in
-- docs/quarantine/ together with its own assertion file, which must NOT be
-- resurrected: its sections 3 and 4 assert the existence-gate semantics this
-- design DELETES, and its expected 525 microUSD is now the wrong number.
-- Read docs/quarantine/README.md before changing anything here.
--
-- The four defects that killed 202607280028 were all REPRODUCED BY EXECUTION,
-- so each gets its own named, separately-failing section below. A section that
-- proves two of them at once would let one regression hide behind the other.
--
--   SECTION 1 (a)  a materially-paid, recent, forward-linked replay IS billable,
--                  at 25% of tokens_saved x the customer's OWN observed price
--   SECTION 2 (b)  an anchor below the $0.30 materiality floor does NOT make
--                  savings billable                          [quarantine defect 2]
--   SECTION 3 (c)  an anchor older than the lookback does NOT [quarantine defect 2]
--   SECTION 4 (d)  a row inserted AFTER a settlement, backdated into its period,
--                  does NOT change that settlement's basis    [quarantine defect 3]
--   SECTION 5 (e)  replay-hammering is capped by the spend ratio
--                                                             [quarantine defect 4]
--   SECTION 6 (f)  provider-native discount rows (cache_attributable = false)
--                  contribute no savings
--   SECTION 7 (g)  savings with NO price source at all still trip
--                  zero_spend_concentration, exactly as before this migration
--   SECTION 8 (h)  the 25% cap, the warm deduction, the cumulative ceiling and
--                  the promote-door 'pending' predicate all still hold
--   SECTION 9      the remaining fail-closed anchor negatives, one flag each
--
-- ARITHMETIC USED THROUGHOUT, so every expected number is checkable by hand.
-- Every fixture prices its ancestor so that ONE PAID DOLLAR BUYS ONE MILLION
-- TOKENS, i.e. observed_unit_cost = 0.000001 USD/token:
--
--   SECTION 1: ancestor  actual_cost_usd = 1.00 over 1,000,000 billed tokens
--              replay    tokens_saved = 500,000, verified_savings_usd = 2.00
--     observed_unit_cost = 1.00 / 1,000,000                    = 0.000001
--     (A) list-price counterfactual                            = 2.00
--     (B) observed reconstruction = 500,000 * 0.000001         = 0.50
--     anchored_savings = least(A, B)                           = 0.50   <- (B) binds
--     spend-ratio cap  = 3.000 * 1.00 = 3.00                   not binding
--     basis = least(0 + 0.50, gross 2.00)                      = 0.50
--     fee   = floor(0.50 * 0.25 * 1e6)                         = 125,000 microUSD
--
--   The 1.50 USD of list-price saving we deliberately leave on the table is the
--   whole point of the owner's decision: the customer's own observed price is
--   BELOW our list table for this fixture, so term (B) binds and the fee falls.
--
-- WHY EVERY WINDOW IS DERIVED FROM now(): two conditions under test
-- (period_not_closed, and the promoter's 34-day Stripe reporting window) are
-- now()-relative, so fixed calendar dates would silently stop exercising them as
-- this file aged. now() is transaction-stable, so the file stays deterministic.
--
-- The trailing ROLLBACK discards every fixture, so this file is re-runnable at
-- any point in the replay and leaves nothing behind in what 202607280007 calls a
-- seven-year financial record. That rollback is not optional:
-- public.period_settlement_ledger carries an UNCONDITIONAL BEFORE DELETE guard,
-- so a settlement written here could not be cleaned up with a DELETE.

begin;

-- The whole file runs under a deliberately NON-UTC session timezone, for the
-- same reason migration-settlement-writer-assertions.sql does: week arithmetic
-- in this system is 604800 FIXED seconds, and if any of it were secretly
-- wall-clock-dependent the settlements below would land on the wrong week here
-- and only here.
set local timezone = 'America/New_York';

-- ---------------------------------------------------------------------------
-- SECTION 0. Preconditions. A missing object here is a REGISTRATION failure
--            (the migration is not in the manifests), not a regression, and it
--            must say so rather than failing later with a confusing type error.
-- ---------------------------------------------------------------------------
do $section_0$
begin
    if to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)'
       ) is null
       or to_regprocedure(
           'public.billing_observed_model_price(uuid,timestamptz,timestamptz,integer,bigint)'
       ) is null
       or not exists (
           select 1 from information_schema.columns
            where table_schema = 'public' and table_name = 'usage_log'
              and column_name = 'savings_anchor_request_id'
       )
       or not exists (
           select 1 from information_schema.columns
            where table_schema = 'public' and table_name = 'billing_halting_conditions'
              and column_name = 'max_cache_savings_per_spend_ratio'
       ) then
        raise exception
            '202607280031 is not applied: the observed-price fee basis is missing. '
            'Register supabase/migrations/202607280031_observed_price_cache_fee_basis.sql '
            'in scripts/ci/migration-fresh-manifest.txt and '
            'scripts/ci/migration-upgrade-manifest.txt. This is a missing-registration '
            'message, not a regression.';
    end if;
    -- The thresholds this whole file's arithmetic assumes.
    if not exists (
        select 1 from public.billing_halting_conditions
         where singleton
           and cache_anchor_materiality_floor_usd = 0.3000
           and cache_anchor_lookback_days = 30
           and max_cache_savings_per_spend_ratio = 3.000
           and max_fee_share_of_verified_savings = 0.25000
           and max_zero_spend_savings_share = 0.50000
    ) then
        raise exception 'the halting thresholds are not at their shipped defaults; every '
                        'expected number in this file is derived from 0.3000 / 30 / 3.000 / '
                        '0.25 / 0.50';
    end if;
end;
$section_0$;

-- ---------------------------------------------------------------------------
-- Fixtures, as pg_temp functions.
--
-- They live in pg_temp rather than public so they cannot outlive the session
-- even if the rollback below were ever removed, and they exist at all because
-- eight self-contained sections would otherwise repeat the same twenty lines of
-- organization/billing-account/attestation setup eight times and drift.
--
-- organization_billing_arrangement has NO product writer by design
-- (202607280009), so these inserts are the same direct superuser inserts
-- 202607280013's own contract probe uses.
-- ---------------------------------------------------------------------------
create function pg_temp.new_billing_org(p_label text)
returns table (org_id uuid, window_start timestamptz, window_end timestamptz)
language plpgsql as $$
declare
    v_owner uuid := gen_random_uuid();
    v_org uuid;
    -- The week that JUST CLOSED. The Stripe anchor is the NEXT period, so
    -- billing_period_for_occurrence reconstructs this window by floor division
    -- and nothing here does local date arithmetic.
    v_end timestamptz := date_trunc('hour', now()) - interval '1 hour';
    v_start timestamptz := v_end - interval '604800 seconds';
begin
    insert into auth.users (id, email)
    values (v_owner, '202607280031-' || p_label || '@example.invalid');
    insert into public.organizations (name, billing_owner_id)
    values ('202607280031 ' || p_label, v_owner)
    returning id into v_org;
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end, stripe_customer_id
    ) values (
        v_org, v_owner, 'active', v_start - interval '1 day',
        v_end, v_end + interval '604800 seconds',
        'cus_' || left(replace(v_org::text, '-', ''), 20)
    );
    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by, attested_evidence)
    values (v_org, 'marginal_per_call', '202607280031 contract',
            'observed-price fee basis assertion fixture');
    return query select v_org, v_start, v_end;
end;
$$;

-- A PAID cache miss: a real provider receipt, priced, through the proxy. This is
-- the row that is both the LINK TARGET (by request_id) and a member of the
-- PRICE SET (by provider+model). A miss saves nothing, so verified_savings_usd
-- is 0 -- exactly what brevitas/receipts.py records for one.
create function pg_temp.paid_miss(
    p_org uuid, p_ts timestamptz, p_request_id text,
    p_cost numeric, p_billed_tokens bigint,
    p_model text default 'deepseek-chat'
) returns bigint
language plpgsql as $$
declare
    v_id bigint;
begin
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, authoritative, pricing_status, receipt_source,
        quality_status, strategy, cache_attributable,
        actual_cost_usd, baseline_cost_usd, verified_savings_usd,
        fresh_input_tokens, output_tokens, tokens_saved,
        savings_anchor_request_id
    ) values (
        '202607280031-probe', '202607280031-probe', p_org, p_ts, p_request_id, 'probe',
        'deepseek', p_model, true, 'priced', 'proxy',
        'verified', '', false,
        p_cost, p_cost, 0,
        p_billed_tokens, 0, 0,
        ''
    ) returning id into v_id;
    return v_id;
end;
$$;

-- A Brevitas EXACT-CACHE REPLAY: zero spend BY CONSTRUCTION
-- (brevitas/proxy.py:649-651 -> brevitas/receipts.py:381-386), attributable to
-- us, carrying the id of the paid request it replays.
create function pg_temp.replay(
    p_org uuid, p_ts timestamptz, p_request_id text, p_anchor text,
    p_tokens_saved bigint, p_verified numeric,
    p_cache_attributable boolean default true,
    p_strategy text default 'exact_cache',
    p_receipt_source text default 'proxy',
    p_model text default 'deepseek-chat',
    p_quality_status text default 'verified'
) returns bigint
language plpgsql as $$
declare
    v_id bigint;
begin
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, authoritative, pricing_status, receipt_source,
        quality_status, strategy, cache_attributable,
        actual_cost_usd, baseline_cost_usd, verified_savings_usd,
        fresh_input_tokens, output_tokens, tokens_saved,
        savings_anchor_request_id
    ) values (
        '202607280031-probe', '202607280031-probe', p_org, p_ts, p_request_id, 'probe',
        'deepseek', p_model, true, 'priced', p_receipt_source,
        p_quality_status, p_strategy, p_cache_attributable,
        0, p_verified, p_verified,
        0, 0, p_tokens_saved,
        p_anchor
    ) returning id into v_id;
    return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- SECTION 1 (a). THE POSITIVE CASE. A materially-paid, recent, forward-linked
--                replay IS billable, at 25% of tokens_saved x the customer's own
--                observed price per token -- and NOT at 25% of the list-price
--                saving, which is 4x larger here.
--
-- This is the section that proves the drought is over. If it fails, the product
-- is still structurally unbillable for every OpenAI-compatible tenant.
-- ---------------------------------------------------------------------------
do $section_1$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_price record;
    v_result jsonb;
    v_row public.period_settlement_ledger%rowtype;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section1-billable');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s1-ancestor', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s1-replay', 'proxy:s1-ancestor', 500000, 2.00);

    -- The price source, on its own, so a failure here is unambiguous.
    select * into v_price
      from public.billing_observed_model_price(
          v_org, v_start, v_end, 30,
          (select max(id) from public.usage_log where organization_id = v_org)
      );
    if v_price.paid_cost_usd <> 1.00
       or v_price.paid_tokens <> 1000000
       or v_price.observed_unit_cost_usd <> 0.000001 then
        raise exception 'SECTION 1(a): the observed unit cost is wrong: %', row_to_json(v_price);
    end if;

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.eligible_rows <> 2 or v_evidence.zero_spend_rows <> 1 then
        raise exception 'SECTION 1(a): wrong eligible set: %', row_to_json(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_rows <> 1
       or v_evidence.unanchored_zero_spend_rows <> 0 then
        raise exception 'SECTION 1(a): the forward link did not classify the replay as '
                        'anchored -- savings_anchor_request_id is not reaching the equijoin: %',
            row_to_json(v_evidence);
    end if;
    -- (B) binds: 500,000 * 0.000001 = 0.50, well under the 2.00 list saving.
    if v_evidence.anchored_zero_spend_savings_usd <> 0.50 then
        raise exception 'SECTION 1(a): anchored savings are %, expected 0.50 (tokens_saved x '
                        'the observed unit cost, NOT the list-price counterfactual)',
            v_evidence.anchored_zero_spend_savings_usd;
    end if;
    if v_evidence.net_verified_savings_usd <> 2.00 then
        raise exception 'SECTION 1(a): the GROSS must stay 2.00 -- the halting gate is '
                        'evaluated on it: %', v_evidence.net_verified_savings_usd;
    end if;
    if v_evidence.billable_savings_basis_usd <> 0.50 then
        raise exception 'SECTION 1(a): the fee basis is %, expected 0.50',
            v_evidence.billable_savings_basis_usd;
    end if;
    -- The frozen guard's numerator saw nothing: this saving HAS a price source.
    if v_evidence.zero_spend_net_savings_usd <> 0 then
        raise exception 'SECTION 1(a): an anchored replay still counts into the '
                        'zero_spend_concentration numerator: %',
            v_evidence.zero_spend_net_savings_usd;
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section1');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'SECTION 1(a): a materially-paid, linked replay did not settle: %',
            v_result;
    end if;
    -- 0.50 * 0.25 * 1e6. The whole point of the migration, in one number.
    if (v_result->>'fee_microusd')::bigint <> 125000 then
        raise exception 'SECTION 1(a): fee is % microUSD, expected 125,000 (25%% of '
                        '500,000 tokens x 0.000001 USD/token)', v_result->>'fee_microusd';
    end if;
    -- The gate ran on the UN-NARROWED gross, which is what keeps a $0-basis
    -- period halting with a named condition instead of drafting a silent $0.
    if (v_result->>'gate_fee_microusd')::bigint <> 500000 then
        raise exception 'SECTION 1(a): the halting gate was not evaluated on the gross: %',
            v_result;
    end if;
    if (v_result->'evidence'->>'zero_spend_share')::numeric <> 0 then
        raise exception 'SECTION 1(a): the concentration share should be 0 for a fully '
                        'anchored period: %', v_result->'evidence';
    end if;

    select * into v_row from public.period_settlement_ledger
     where id = (v_result->>'settlement_id')::bigint;
    if v_row.verified_savings_usd <> 0.50 then
        raise exception 'SECTION 1(a): the ledger recorded % as verified_savings_usd; since '
                        '202607280031 that column is the BILLABLE BASIS', v_row.verified_savings_usd;
    end if;
    if v_row.status <> 'draft' then
        raise exception 'SECTION 1(a): the writer produced status %, not draft', v_row.status;
    end if;
    -- The fee sits exactly on period_settlement_ledger's own CHECK boundary,
    -- re-derived from the row's recorded columns and nothing else. That is the
    -- intended shape, and it is what makes the row independently auditable.
    if v_row.fee_microusd <> floor(least(
            greatest(v_row.verified_savings_usd - v_row.warm_spend_usd, 0) * 0.25,
            greatest(v_row.verified_savings_usd, 0) * 0.25) * 1000000)::bigint then
        raise exception 'SECTION 1(a): the recorded fee is not re-derivable from the '
                        'recorded evidence: %', row_to_json(v_row);
    end if;
end;
$section_1$;

-- ---------------------------------------------------------------------------
-- SECTION 2 (b). GUARD 1, THE MATERIALITY FLOOR. Quarantine defect 2, reproduced
--                by execution: "a single $0.0000000001 ancestor anchored $500 of
--                replays into a $125 fee".
--
-- The fixture is SECTION 1's, with ONE difference: the ancestor's TOTAL is 0.20
-- instead of 1.00. The observed unit cost is deliberately IDENTICAL (0.20 over
-- 200,000 tokens is still 0.000001 USD/token), so the floor is the only thing
-- that can change the outcome. It must change it completely: not a smaller fee,
-- NO fee, because a sub-floor tuple has no price at all and every replay of it
-- falls out of the basis.
-- ---------------------------------------------------------------------------
do $section_2$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_result jsonb;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section2-floor');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s2-ancestor', 0.20, 200000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s2-replay', 'proxy:s2-ancestor', 500000, 2.00);

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.observed_priced_models <> 0 then
        raise exception 'SECTION 2(b): a (provider, model) below the $0.30 materiality floor '
                        'still produced a price: %', row_to_json(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.anchored_zero_spend_savings_usd <> 0 then
        raise exception 'SECTION 2(b): a $0.20 ancestor anchored a replay. This is the '
                        'quarantined 202607280028 defect where $1e-10 unlocked $125: %',
            row_to_json(v_evidence);
    end if;
    if v_evidence.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 2(b): the fee basis is %, expected 0 -- a sub-floor tuple is '
                        'OUT of the basis, not billed at a reduced rate',
            v_evidence.billable_savings_basis_usd;
    end if;
    -- ...and the saving lands back in the concentration guard's numerator,
    -- which is where an unanchored zero-spend saving belongs.
    if v_evidence.unanchored_zero_spend_rows <> 1
       or v_evidence.zero_spend_net_savings_usd <> 2.00 then
        raise exception 'SECTION 2(b): an unanchored replay escaped the concentration '
                        'numerator: %', row_to_json(v_evidence);
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section2');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'SECTION 2(b): a sub-floor period did not halt: %', v_result;
    end if;
    if (v_result->>'recomputed_fee_microusd')::bigint <> 0 then
        raise exception 'SECTION 2(b): a sub-floor period derived a non-zero fee: %', v_result;
    end if;
    if exists (select 1 from public.period_settlement_ledger where organization_id = v_org) then
        raise exception 'SECTION 2(b): a halt wrote a settlement row';
    end if;
end;
$section_2$;

-- ---------------------------------------------------------------------------
-- SECTION 3 (c). GUARD 2, THE LOOKBACK. 202607280028 made the lookback
--                DELIBERATELY UNBOUNDED, reasoning that "a cache entry
--                legitimately outlives a billing period". That is wrong on its
--                own facts: 202607170002_cache_security.sql:31-33 DELETES any
--                entry with expires_at > created_at + 24 hours, so a genuine
--                ancestor is ALWAYS within 24 hours of its replay.
--
-- The fixture is SECTION 1's, with ONE difference: the replay happens 5 days
-- after its ancestor, and the lookback is TIGHTENED to 1 day -- a direction the
-- CHECK permits precisely so an operator can do it during an incident. The
-- ancestor stays inside the window and inside the PRICE set (materiality still
-- passes, observed_priced_models is still 1), so the LINK's lookback is the only
-- thing that can change the outcome.
-- ---------------------------------------------------------------------------
do $section_3$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_result jsonb;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section3-lookback');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s3-ancestor', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '5 days',
                           'proxy:s3-replay', 'proxy:s3-ancestor', 500000, 2.00);

    -- Control: at the shipped 30-day lookback this exact shape IS billable.
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.anchored_zero_spend_rows <> 1
       or v_evidence.billable_savings_basis_usd <> 0.50 then
        raise exception 'SECTION 3(c) control: the 5-day-old link should still hold at the '
                        'shipped 30-day lookback: %', row_to_json(v_evidence);
    end if;

    update public.billing_halting_conditions
       set cache_anchor_lookback_days = 1 where singleton;

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    -- The price set is UNCHANGED: the ancestor is still inside [start - 1 day,
    -- end) and still clears materiality. Only the LINK broke.
    if v_evidence.observed_priced_models <> 1 then
        raise exception 'SECTION 3(c): the shortened lookback also removed the price source, '
                        'so this test no longer isolates the link: %', row_to_json(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.anchored_zero_spend_savings_usd <> 0
       or v_evidence.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 3(c): an ancestor older than the lookback still anchored a '
                        'replay: %', row_to_json(v_evidence);
    end if;
    if v_evidence.unanchored_zero_spend_rows <> 1
       or v_evidence.zero_spend_net_savings_usd <> 2.00 then
        raise exception 'SECTION 3(c): the out-of-lookback replay escaped the concentration '
                        'numerator: %', row_to_json(v_evidence);
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '6 days', 'ci:202607280031-section3');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'SECTION 3(c): an out-of-lookback period did not halt: %', v_result;
    end if;

    update public.billing_halting_conditions
       set cache_anchor_lookback_days = 30 where singleton;
end;
$section_3$;

-- ---------------------------------------------------------------------------
-- SECTION 4 (d). NON-RETROACTIVITY. Quarantine defect 3, whose signature is its
--                LAST clause: "fee went 2,500,000 -> 3,500,000 microUSD with the
--                watermark AND row count UNCHANGED". That combination is what
--                makes a settlement irreproducible.
--
-- Root cause: the quarantined anchor lookup bounded the ancestor by TIME ONLY,
-- and ts is WRITER-SUPPLIED (api/store.py:755). usage_log.id is not: it is
-- `generated always as identity`, assigned by the server at INSERT, and
-- impossible to supply, backdate or update. So a row inserted after settlement
-- necessarily lands ABOVE the watermark no matter what ts it claims.
--
-- The backdated row here is not a subtle one: it TRIPLES the organization's paid
-- spend at a HIGHER unit cost, which under the quarantined design would have
-- doubled the fee.
-- ---------------------------------------------------------------------------
do $section_4$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_result jsonb;
    v_settlement_id bigint;
    v_watermark bigint;
    v_rows bigint;
    v_pinned record;
    v_fresh record;
    v_row public.period_settlement_ledger%rowtype;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section4-retroactivity');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s4-ancestor', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s4-replay', 'proxy:s4-ancestor', 500000, 2.00);

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section4');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 125000 then
        raise exception 'SECTION 4(d): the baseline settlement is wrong: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;
    v_watermark := (v_result->>'usage_log_watermark_id')::bigint;
    v_rows := (v_result->>'usage_row_count')::bigint;
    if v_watermark is null then
        raise exception 'SECTION 4(d): the settlement recorded no watermark, so its basis is '
                        'not reproducible at all';
    end if;

    -- THE ATTACK: a paid row inserted AFTER the settlement, backdated into the
    -- settled window, at a unit cost twice the settled one. Its id is
    -- necessarily above the watermark.
    perform pg_temp.paid_miss(v_org, v_start + interval '90 minutes',
                              'proxy:s4-backdated', 3.00, 1000000);

    select * into v_pinned
      from public.billing_period_settlement_evidence(v_org, v_start, v_end, v_watermark);
    if v_pinned.usage_log_watermark_id <> v_watermark then
        raise exception 'SECTION 4(d): the evidence function did not honour the supplied '
                        'watermark: %', row_to_json(v_pinned);
    end if;
    if v_pinned.eligible_rows <> v_rows then
        raise exception 'SECTION 4(d): the pinned eligible set moved: % vs %',
            v_pinned.eligible_rows, v_rows;
    end if;
    if v_pinned.billable_savings_basis_usd <> 0.50
       or v_pinned.actual_spend_usd <> 1.00
       or v_pinned.anchored_zero_spend_savings_usd <> 0.50 then
        raise exception 'SECTION 4(d): A BACKDATED ROW CHANGED AN ALREADY-SETTLED BASIS. '
                        'This is quarantine defect 3 verbatim: %', row_to_json(v_pinned);
    end if;
    -- The recomputed fee is byte-identical to the settled one.
    if floor(greatest(v_pinned.billable_savings_basis_usd
                      - greatest(v_pinned.warm_spend_usd, 0), 0)
             * 0.25 * 1000000)::bigint <> 125000 then
        raise exception 'SECTION 4(d): the pinned basis no longer re-derives the settled fee';
    end if;

    -- ...and the settled row itself never moved.
    select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
    if v_row.fee_microusd <> 125000
       or v_row.verified_savings_usd <> 0.50
       or v_row.usage_log_watermark_id <> v_watermark then
        raise exception 'SECTION 4(d): the settled row itself changed: %', row_to_json(v_row);
    end if;

    -- THE OTHER HALF, and the reason this is honest rather than merely frozen:
    -- with NO watermark the function DOES see the late arrival, and the
    -- watermark, the row count AND the fee all move TOGETHER. A revision is an
    -- append with its own pin, never a silent edit.
    select * into v_fresh
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_fresh.usage_log_watermark_id <= v_watermark
       or v_fresh.eligible_rows <= v_rows then
        raise exception 'SECTION 4(d): an unpinned recomputation did not see the late row: %',
            row_to_json(v_fresh);
    end if;
    -- paid 4.00 over 2,000,000 tokens => 0.000002/token; 500,000 * 0.000002 = 1.00.
    if v_fresh.billable_savings_basis_usd <> 1.00 or v_fresh.actual_spend_usd <> 4.00 then
        raise exception 'SECTION 4(d): the unpinned recomputation is wrong, so the pinned one '
                        'proves nothing: %', row_to_json(v_fresh);
    end if;
end;
$section_4$;

-- ---------------------------------------------------------------------------
-- SECTION 4b (d, continued). THE SHARPER FORM OF THE SAME ATTACK: the backdated
--                row IS THE ANCESTOR, not merely a member of the price set.
--
-- Section 4 above bounds the PRICE SET by the watermark. This one bounds the
-- LINK, which is the clause docs/quarantine/README.md line 27 is literally about
-- ("a paid row inserted AFTER settlement but backdated into the period
-- RETROACTIVELY RE-ANCHORS already-settled replays"). Without
-- `and ancestor.id <= watermark.mark` in the exists(), a replay that was
-- correctly unanchored at settlement time silently becomes billable afterwards,
-- and section 4's price-set bound does NOT catch that -- verified by deleting
-- exactly that clause and watching section 4 stay green.
--
-- No settlement is written here: this shape halts at settlement time (its
-- savings are 100% unanchored, share 1.00000), which is itself the correct
-- outcome. The watermark is taken directly, which is what the evidence function
-- would have recorded.
-- ---------------------------------------------------------------------------
do $section_4b$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_mark bigint;
    v_pinned record;
    v_fresh record;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section4b-late-ancestor');

    -- A material price source that is NOT the named ancestor, so the replay
    -- fails on the LINK alone and not for want of a price.
    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s4b-priced', 1.00, 1000000);
    -- ...and a replay naming an ancestor that DOES NOT EXIST YET.
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s4b-replay', 'proxy:s4b-late', 500000, 2.00);

    v_mark := (select max(id) from public.usage_log where organization_id = v_org);

    select * into v_pinned
      from public.billing_period_settlement_evidence(v_org, v_start, v_end, v_mark);
    if v_pinned.anchored_zero_spend_rows <> 0
       or v_pinned.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 4b(d): a replay naming a non-existent ancestor was billable '
                        'before the attack even started: %', row_to_json(v_pinned);
    end if;

    -- THE ATTACK: the named ancestor appears AFTER the fact, backdated to before
    -- its own replay. Its ts is a lie the writer controls; its id is not.
    perform pg_temp.paid_miss(v_org, v_start + interval '30 minutes',
                              'proxy:s4b-late', 1.00, 1000000);

    select * into v_pinned
      from public.billing_period_settlement_evidence(v_org, v_start, v_end, v_mark);
    if v_pinned.anchored_zero_spend_rows <> 0
       or v_pinned.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 4b(d): A BACKDATED ANCESTOR RETROACTIVELY RE-ANCHORED AN '
                        'ALREADY-EVALUATED REPLAY. This is quarantine defect 3 in its exact '
                        'recorded form: %', row_to_json(v_pinned);
    end if;

    -- ...and, so this is not vacuous, the same row DOES anchor once the
    -- watermark legitimately moves past it. A revision is an append.
    select * into v_fresh
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_fresh.anchored_zero_spend_rows <> 1
       or v_fresh.billable_savings_basis_usd <> 0.50 then
        raise exception 'SECTION 4b(d): the late ancestor never anchors at all, so the pinned '
                        'refusal above proves nothing: %', row_to_json(v_fresh);
    end if;
end;
$section_4b$;

-- ---------------------------------------------------------------------------
-- SECTION 5 (e). GUARD 3, THE SPEND-RATIO CAP. Replay-hammering: one paid call
--                fills a cache entry, then a loop replays it. Every replay is
--                genuinely linked, genuinely verified, genuinely
--                cache_attributable -- the LINK and the MATERIALITY FLOOR both
--                PASS. Nothing else in this design bounds it, because nothing
--                else relates savings to spend.
--
-- Fixture: ancestor 0.40 over 400,000 tokens (unit cost 0.000001, materiality
-- clears 0.30), then TEN replays of 1,000,000 tokens each at a 5.00 list saving.
--   per replay: least(5.00, 1,000,000 * 0.000001 = 1.00) = 1.00
--   raw        = 10 * 1.00                                = 10.00
--   cap        = 3.000 * 0.40                             =  1.20   <- binds
--   basis      = least(0 + 1.20, gross 50.00)             =  1.20
--   fee        = floor(1.20 * 0.25 * 1e6)                 = 300,000 microUSD
-- Uncapped this would have been floor(10.00 * 0.25 * 1e6) = 2,500,000 microUSD
-- against FORTY CENTS of provider spend.
-- ---------------------------------------------------------------------------
do $section_5$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_result jsonb;
    v_index integer;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section5-hammering');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s5-ancestor', 0.40, 400000);
    for v_index in 1..10 loop
        perform pg_temp.replay(
            v_org, v_start + interval '2 hours' + (v_index * interval '1 minute'),
            'proxy:s5-replay-' || v_index, 'proxy:s5-ancestor', 1000000, 5.00);
    end loop;

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.anchored_zero_spend_rows <> 10 then
        raise exception 'SECTION 5(e): the replays are not all genuinely linked, so this test '
                        'is not measuring the cap: %', row_to_json(v_evidence);
    end if;
    -- The diagnostic ratio is PRE-cap, so it tells an operator how far past the
    -- ceiling this period actually sat: 10.00 / 0.40 = 25.
    if v_evidence.cache_savings_spend_ratio <> 25.000000 then
        raise exception 'SECTION 5(e): the pre-cap diagnostic ratio is %, expected 25.000000',
            v_evidence.cache_savings_spend_ratio;
    end if;
    if v_evidence.anchored_zero_spend_savings_usd <> 1.20 then
        raise exception 'SECTION 5(e): REPLAY-HAMMERING WAS NOT CAPPED. anchored savings are '
                        '%, expected 3.000 x actual spend 0.40 = 1.20',
            v_evidence.anchored_zero_spend_savings_usd;
    end if;
    if v_evidence.anchored_zero_spend_savings_usd
       <> 3.000 * v_evidence.actual_spend_usd then
        raise exception 'SECTION 5(e): the cap is not the configured ratio times the '
                        'recomputed spend: %', row_to_json(v_evidence);
    end if;
    if v_evidence.billable_savings_basis_usd <> 1.20 then
        raise exception 'SECTION 5(e): the fee basis is %, expected 1.20',
            v_evidence.billable_savings_basis_usd;
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section5');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 300000 then
        raise exception 'SECTION 5(e): fee is %, expected 300,000 microUSD (25%% of the '
                        'capped 1.20 basis, not of the uncapped 10.00)', v_result;
    end if;

    -- Tightening the ratio is an UPDATE, and it must lower the basis immediately.
    update public.billing_halting_conditions
       set max_cache_savings_per_spend_ratio = 1.000 where singleton;
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.anchored_zero_spend_savings_usd <> 0.40 then
        raise exception 'SECTION 5(e): the ratio cap is not read from '
                        'billing_halting_conditions at evaluation time: %',
            row_to_json(v_evidence);
    end if;
    update public.billing_halting_conditions
       set max_cache_savings_per_spend_ratio = 3.000 where singleton;
end;
$section_5$;

-- ---------------------------------------------------------------------------
-- SECTION 6 (f). PROVIDER-NATIVE CACHE DISCOUNTS STAY UNBILLABLE. A DeepSeek
--                prompt_cache_hit_tokens row -- or any provider-side or
--                caller-side discount -- carries cache_attributable = false.
--                WE DID NOT CAUSE IT, so we do not bill for it.
--
-- This is the ISOLATION TEST: the fixture is SECTION 1's, byte for byte, with
-- ONE FLAG flipped. Same rows, one boolean, settle vs halt. That pair is the
-- whole change, isolated, and it is requirement 6 PROVEN rather than asserted.
-- ---------------------------------------------------------------------------
do $section_6$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_result jsonb;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section6-native-discount');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s6-ancestor', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s6-replay', 'proxy:s6-ancestor', 500000, 2.00,
                           false);   -- <- the ONE difference from SECTION 1

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    -- The price source is unchanged and still material, so nothing but the flag
    -- can explain the difference.
    if v_evidence.observed_priced_models <> 1 then
        raise exception 'SECTION 6(f): the fixture lost its price source, so the flag is not '
                        'isolated: %', row_to_json(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 6(f): a provider-NATIVE cache discount entered the fee basis. '
                        'Brevitas did not cause that saving: %', row_to_json(v_evidence);
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section6');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'SECTION 6(f): the same rows that settle with cache_attributable = '
                        'true must HALT with it false: %', v_result;
    end if;
end;
$section_6$;

-- ---------------------------------------------------------------------------
-- SECTION 7 (g). THE GUARD STILL CATCHES THE SHAPE IT WAS BUILT FOR. Savings
--                with NO price source behind them AT ALL -- no anchor, no
--                linked ancestor, nothing -- must still sit at share 1.00000 and
--                still halt, exactly as they do today. Requirement 7.
--
-- This is also the NEGATIVE CONTROL for every row written before 202607280031
-- ships: the ALTER backfills savings_anchor_request_id with '', an empty anchor
-- is UNANCHORED full stop, and there is no weaker fallback to "same org + same
-- provider + same model + any earlier paid row". That path is DELETED.
-- ---------------------------------------------------------------------------
do $section_7$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_result jsonb;
    v_zero_org uuid;
    v_zero_start timestamptz;
    v_zero_end timestamptz;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section7-no-price-source');

    -- A paid miss, so the period HAS provider spend and the coarser
    -- halting_condition=zero_spend cannot fire instead. The replay has no
    -- anchor at all -- exactly the shape of every row written before this
    -- migration.
    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s7-ancestor', 1.00, 1000000);
    -- A PAID PROXY RECEIPT WITH AN EMPTY request_id. usage_log.request_id
    -- defaults to '' and 202607280026's uniqueness index explicitly excludes
    -- empty ids, so such rows exist and there can be many of them. If the
    -- evidence function ever stops requiring savings_anchor_request_id <> '',
    -- ONE of these anchors EVERY unanchored replay the organization ever wrote,
    -- because '' = '' -- including every row written before this migration
    -- shipped. That is the single worst way this design could fail, so the
    -- fixture contains one on purpose.
    perform pg_temp.paid_miss(v_org, v_start + interval '70 minutes',
                              '', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s7-replay', '', 500000, 2.00);

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.anchored_zero_spend_rows <> 0 then
        raise exception 'SECTION 7(g): AN EMPTY ANCHOR MATCHED AN EMPTY request_id. Every '
                        'pre-202607280031 row carries '''' in both columns, so this would make '
                        'the entire back catalogue billable at once: %', row_to_json(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.billable_savings_basis_usd <> 0 then
        raise exception 'SECTION 7(g): an anchorless replay became billable. There is NO '
                        'fallback to org+provider+model: %', row_to_json(v_evidence);
    end if;
    if v_evidence.zero_spend_net_savings_usd <> v_evidence.net_verified_savings_usd then
        raise exception 'SECTION 7(g): the concentration numerator no longer sees savings '
                        'with no price source: %', row_to_json(v_evidence);
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '3 hours', 'ci:202607280031-section7');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'SECTION 7(g): 202607280008''s tripwire stopped firing for the shape '
                        'it was built for: %', v_result;
    end if;
    -- Share 1.00000 against the 0.50000 limit -- verbatim the caestus halt this
    -- whole migration exists to explain.
    if (v_result->>'message') not like '%zero_spend_concentration: 1.00000%' then
        raise exception 'SECTION 7(g): the halt no longer reports share 1.00000: %',
            v_result->>'message';
    end if;
    -- The gate ran on the gross (500,000 microUSD) while the basis derived $0.
    -- That pair is the whole reason the gate is NOT evaluated on the basis: a
    -- $0 fee would have skipped every zero-spend test and drafted a silent $0.
    if (v_result->>'recomputed_fee_microusd')::bigint <> 0
       or (v_result->>'gate_fee_microusd')::bigint <> 500000 then
        raise exception 'SECTION 7(g): the gate/basis split is wrong: %', v_result;
    end if;

    -- And the coarser condition is still reachable: savings with no provider
    -- spend ANYWHERE in the period halt on zero_spend, not on concentration.
    select org_id, window_start, window_end into v_zero_org, v_zero_start, v_zero_end
      from pg_temp.new_billing_org('section7-no-spend');
    perform pg_temp.replay(v_zero_org, v_zero_start + interval '2 hours',
                           'proxy:s7b-replay', '', 500000, 2.00);
    v_result := public.settle_billing_period(
        v_zero_org, v_zero_start + interval '3 hours', 'ci:202607280031-section7b');
    if v_result->>'halting_condition' <> 'zero_spend' then
        raise exception 'SECTION 7(g): a period with no provider spend at all no longer halts '
                        'on zero_spend: %', v_result;
    end if;
end;
$section_7$;

-- ---------------------------------------------------------------------------
-- SECTION 8 (h). EVERYTHING THIS MIGRATION PROMISED NOT TO TOUCH.
--
--   * the 25% rate, read from billing_halting_conditions
--   * the warm deduction, taken from the SMALLER basis
--   * the cumulative period ceiling in the frozen 202607280008 guard
--   * step 9's promote-door predicate, with 'pending' PRESENT
--
-- The fixture is deliberately CACHE-FREE -- one spend-backed row, verified 100,
-- actual cost 40, warm 10 -- so the basis equals the gross and the numbers are
-- the SAME 22,500,000 microUSD 202607280013's own contract probe asserts. If
-- this migration had disturbed the spend-backed path, this section moves.
-- ---------------------------------------------------------------------------
do $section_8$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_warm_day date;
    v_evidence record;
    v_result jsonb;
    v_settlement_id bigint;
    v_message text;
    v_condition text;
    v_summary jsonb;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section8-invariants');
    v_warm_day := ((v_start at time zone 'UTC') + interval '1 day')::date;

    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        '202607280031-probe', '202607280031-probe', v_org,
        v_start + interval '1 hour', 'proxy:s8-compression', 'probe',
        'deepseek', 'deepseek-chat', 100, 40, 140, true, 'priced', 'proxy'
    );
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_org, 'anthropic', v_warm_day, 10);

    -- Spend-backed savings are billable on their own evidence and were never in
    -- question: cache_attributable is NOT consulted for them.
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.billable_savings_basis_usd <> 100
       or v_evidence.net_verified_savings_usd <> 100
       or v_evidence.warm_spend_usd <> 10
       or v_evidence.warm_spend_days <> 1
       or v_evidence.zero_spend_rows <> 0 then
        raise exception 'SECTION 8(h): the spend-backed path moved: %', row_to_json(v_evidence);
    end if;

    v_result := public.settle_billing_period(
        v_org, v_start + interval '2 hours', 'ci:202607280031-section8');
    -- 25% of (100 - 10). The warm deduction is taken from the basis, and the
    -- rate is the one in the thresholds table.
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 22500000 then
        raise exception 'SECTION 8(h): the 25%% rate or the warm deduction changed: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;

    -- The read path converges on the SAME basis the writer records, so the
    -- billing screen does not drop a number on settlement day.
    v_summary := public.billing_period_settlement_summary(v_org, v_end);
    if (v_summary->>'ok')::boolean is not true
       or v_summary->'evidence'->'billable_savings_basis_usd' is null
       or v_summary->'evidence'->'cache_savings_spend_ratio' is null then
        raise exception 'SECTION 8(h): the status summary lost the basis keys: %', v_summary;
    end if;

    -- THE PROMOTE DOOR. A draft becomes billable only through this function, and
    -- once it is 'pending' the money IS queued.
    v_result := public.promote_billing_period_settlement(
        v_settlement_id, 'ci:202607280031', 'section 8 promote-door contract');
    if v_result->>'outcome' <> 'promoted'
       or v_result->>'settlement_status' <> 'pending' then
        raise exception 'SECTION 8(h): promotion of a valid draft failed: %', v_result;
    end if;

    -- Step 9's predicate must still contain 'pending'. Without it, settle ->
    -- promote -> revise -> promote queues TWO charges for one period and Stripe
    -- deduplicates nothing, because the idempotency keys differ.
    --
    -- A late receipt is what makes this reachable at all: step 8's idempotence
    -- check runs BEFORE step 9 (deliberately, so an unchanged period reports
    -- 'unchanged' even after promotion), so the evidence has to actually move
    -- or the revision never gets as far as the committed-money refusal.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        '202607280031-probe', '202607280031-probe', v_org,
        v_start + interval '90 minutes', 'proxy:s8-late', 'probe',
        'deepseek', 'deepseek-chat', 20, 8, 28, true, 'priced', 'proxy'
    );
    v_result := public.settle_billing_period(
        v_org, v_start + interval '2 hours', 'ci:202607280031-section8-revise', true);
    if v_result->>'code' <> 'period_already_committed' then
        raise exception 'SECTION 8(h): the promote-door predicate lost ''pending'', so a '
                        'promoted period can be settled and billed again: %', v_result;
    end if;

    -- THE CUMULATIVE CEILING, in the frozen guard, against the NARROWED
    -- evidence. Mark the promoted row as actually sent, then ask the guard for
    -- the same fee again: 22,500,000 already committed plus another 22,500,000
    -- is above the 22,500,000 ceiling.
    update public.period_settlement_ledger
       set status = 'sending', outbound_started_at = now(),
           lease_owner = 'ci', lease_expires_at = now() + interval '5 minutes'
     where id = v_settlement_id;

    begin
        perform public.assert_billing_period_settlement_allowed(
            v_org, v_start, v_end, 22500000::bigint);
        raise exception 'SECTION 8(h): the cumulative period ceiling no longer fires';
    exception
        when sqlstate '55000' then
            get stacked diagnostics v_message = message_text;
            v_condition := substring(v_message from 'halting_condition=([a-z_]+)');
            if v_condition <> 'cumulative_ceiling' then
                raise exception 'SECTION 8(h): expected cumulative_ceiling, got %: %',
                    v_condition, v_message;
            end if;
    end;
end;
$section_8$;

-- ---------------------------------------------------------------------------
-- SECTION 9. THE REMAINING FAIL-CLOSED NEGATIVES, one flag each.
--
-- Every degradation in the whole anchoring chain yields '' or false and bills
-- zero: a missing cache column, a tripped SemanticCache._anchor_supported latch,
-- a PGRST204 retry in api/store.py, a caller-supplied claim over POST /v1/usage,
-- a batch import that drops the column outright (api/store.py:4504). The SQL
-- side must refuse each of them independently, because api/server.py's
-- _decide_savings_anchor and api/store.py's _savings_anchor are the OTHER two
-- floors and this one must not depend on either.
--
-- Every case below is SECTION 1's billable fixture with exactly one thing
-- changed, so a pass here means that one thing is load-bearing.
-- ---------------------------------------------------------------------------
do $section_9$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
    v_case record;
    v_ancestor_id text;
    v_other_paid_id text;
    v_replay_id text;
    v_other_org uuid;
begin
    -- The FIRST case is a control that must anchor. Without it this loop could
    -- pass by being vacuous -- a fixture broken in some unrelated way refuses
    -- every case for the wrong reason and still reports six green negatives.
    for v_case in
        select * from (values
            ('control', 'nothing is wrong with this one; it MUST bill', true),
            ('self-referential', 'the replay names ITSELF as its ancestor', false),
            ('non-replay-strategy', 'the strategy is not a Brevitas cache replay', false),
            ('sdk-receipt', 'the receipt came from the SDK, not through our proxy', false),
            ('unverified-quality', 'the replay''s quality was never verified', false),
            ('unauthoritative-ancestor',
             'the named ancestor is caller-reported telemetry, which is never evidence '
             'of a payment', false),
            ('unpaid-ancestor', 'the named ancestor recorded no provider spend', false),
            ('missing-ancestor', 'the named ancestor does not exist', false),
            ('cross-org-ancestor',
             'the named ancestor belongs to a DIFFERENT organization', false)
        ) as cases(label, why, expect_anchored)
    loop
        -- usage_log_request_authority_unique is (key_hash, request_id,
        -- authoritative) since 202607280026, and every case here shares one
        -- key_hash, so the ids have to differ per case.
        v_ancestor_id := 'proxy:s9-' || v_case.label || '-ancestor';
        v_other_paid_id := 'proxy:s9-' || v_case.label || '-other-paid';
        v_replay_id := 'proxy:s9-' || v_case.label || '-replay';

        select org_id, window_start, window_end into v_org, v_start, v_end
          from pg_temp.new_billing_org('section9-' || v_case.label);

        if v_case.label = 'unauthoritative-ancestor' then
            insert into public.usage_log (
                key_hash, owner_id, organization_id, ts, request_id, project,
                provider, model, authoritative, pricing_status, receipt_source,
                quality_status, actual_cost_usd, baseline_cost_usd,
                verified_savings_usd, fresh_input_tokens
            ) values (
                '202607280031-probe-claim', '202607280031-probe', v_org,
                v_start + interval '1 hour', v_ancestor_id, 'probe',
                'deepseek', 'deepseek-chat', false, 'priced', 'proxy',
                'verified', 1.00, 1.00, 0, 1000000
            );
            -- A materially-paid AUTHORITATIVE row for the same model, so the
            -- price set is non-empty and only the LINK can fail.
            perform pg_temp.paid_miss(v_org, v_start + interval '70 minutes',
                                      v_other_paid_id, 1.00, 1000000);
        elsif v_case.label = 'unpaid-ancestor' then
            insert into public.usage_log (
                key_hash, owner_id, organization_id, ts, request_id, project,
                provider, model, authoritative, pricing_status, receipt_source,
                quality_status, actual_cost_usd, baseline_cost_usd,
                verified_savings_usd, fresh_input_tokens
            ) values (
                '202607280031-probe', '202607280031-probe', v_org,
                v_start + interval '1 hour', v_ancestor_id, 'probe',
                'deepseek', 'deepseek-chat', true, 'priced', 'proxy',
                'verified', 0, 0, 0, 1000000
            );
            perform pg_temp.paid_miss(v_org, v_start + interval '70 minutes',
                                      v_other_paid_id, 1.00, 1000000);
        elsif v_case.label = 'missing-ancestor' then
            perform pg_temp.paid_miss(v_org, v_start + interval '70 minutes',
                                      v_other_paid_id, 1.00, 1000000);
        elsif v_case.label = 'cross-org-ancestor' then
            -- The named ancestor is real, authoritative, paid and recent -- it
            -- just belongs to SOMEBODY ELSE. request_id is unique only per
            -- (key_hash, request_id, authoritative) since 202607280026, so ids
            -- genuinely collide across organizations and the equijoin MUST be
            -- scoped by organization_id.
            select org_id into v_other_org
              from pg_temp.new_billing_org('section9-cross-org-neighbour');
            perform pg_temp.paid_miss(v_other_org, v_start + interval '1 hour',
                                      v_ancestor_id, 1.00, 1000000);
            perform pg_temp.paid_miss(v_org, v_start + interval '70 minutes',
                                      v_other_paid_id, 1.00, 1000000);
        else
            perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                                      v_ancestor_id, 1.00, 1000000);
        end if;

        if v_case.label = 'self-referential' then
            -- A PAID row that shares the replay's request_id under a different
            -- key_hash. Without it the self-reference check would be untestable
            -- (a replay costs $0, so it can never satisfy actual_cost_usd > 0
            -- against itself) and the clause would look load-bearing while
            -- protecting nothing. With it, the ONLY thing standing between this
            -- replay and a fee is `savings_anchor_request_id <> request_id`.
            insert into public.usage_log (
                key_hash, owner_id, organization_id, ts, request_id, project,
                provider, model, authoritative, pricing_status, receipt_source,
                quality_status, actual_cost_usd, baseline_cost_usd,
                verified_savings_usd, fresh_input_tokens
            ) values (
                '202607280031-probe-sibling', '202607280031-probe', v_org,
                v_start + interval '80 minutes', v_replay_id, 'probe',
                'deepseek', 'deepseek-chat', true, 'priced', 'proxy',
                'verified', 1.00, 1.00, 0, 1000000
            );
            perform pg_temp.replay(v_org, v_start + interval '2 hours',
                                   v_replay_id, v_replay_id, 500000, 2.00);
        elsif v_case.label = 'unverified-quality' then
            perform pg_temp.replay(v_org, v_start + interval '2 hours',
                                   v_replay_id, v_ancestor_id, 500000, 2.00,
                                   true, 'exact_cache', 'proxy', 'deepseek-chat',
                                   'unverified');
        elsif v_case.label = 'non-replay-strategy' then
            perform pg_temp.replay(v_org, v_start + interval '2 hours',
                                   v_replay_id, v_ancestor_id, 500000, 2.00,
                                   true, 'token_compression');
        elsif v_case.label = 'sdk-receipt' then
            perform pg_temp.replay(v_org, v_start + interval '2 hours',
                                   v_replay_id, v_ancestor_id, 500000, 2.00,
                                   true, 'exact_cache', 'sdk');
        else
            perform pg_temp.replay(v_org, v_start + interval '2 hours',
                                   v_replay_id, v_ancestor_id, 500000, 2.00);
        end if;

        select * into v_evidence
          from public.billing_period_settlement_evidence(v_org, v_start, v_end);
        -- Sanity: a price source EXISTS in every case, so nothing but the one
        -- changed thing can explain the refusal.
        if v_evidence.observed_priced_models <> 1 then
            raise exception 'SECTION 9 [%]: the fixture has no price source, so it proves '
                            'nothing', v_case.label;
        end if;
        if v_case.expect_anchored then
            if v_evidence.anchored_zero_spend_rows <> 1
               or v_evidence.billable_savings_basis_usd <> 0.50 then
                raise exception 'SECTION 9 [%]: THE CONTROL DID NOT BILL, so every negative '
                                'below it is vacuous -- %. evidence: %',
                    v_case.label, v_case.why, row_to_json(v_evidence);
            end if;
        elsif v_evidence.anchored_zero_spend_rows <> 0
           or v_evidence.billable_savings_basis_usd <> 0 then
            raise exception 'SECTION 9 [%]: BILLED ANYWAY -- %. evidence: %',
                v_case.label, v_case.why, row_to_json(v_evidence);
        end if;
    end loop;
end;
$section_9$;

-- ---------------------------------------------------------------------------
-- SECTION 10. THE BASIS CAN NEVER EXCEED THE PERIOD'S OWN GROSS NETTED SAVINGS.
--
-- The basis is least(spend_backed + anchored_capped, net_verified). For a
-- normal period that least() is slack -- anchored_savings(r) is already <=
-- r.verified_savings_usd row by row, so the sum cannot overshoot. It becomes
-- LOAD-BEARING exactly when some UNANCHORED row carries NEGATIVE verified
-- savings, i.e. a netting correction: without the floor, an organization whose
-- corrections wiped out its own savings would still be billed on the
-- uncorrected anchored component, and the settlement row would record a basis
-- LARGER than the period's gross -- a number no independent recomputation could
-- reproduce.
--
--   ancestor    1.00 over 1,000,000 tokens          => 0.000001 USD/token
--   replay      tokens_saved 5,000,000, verified 2.00
--     (B) 5,000,000 * 0.000001 = 5.00, so (A) binds: anchored = 2.00
--   correction  an unanchored zero-spend row at -1.00
--   gross  = 0 + 2.00 - 1.00                                    = 1.00
--   raw    = spend_backed 0 + anchored 2.00                     = 2.00
--   basis  = least(2.00, 1.00)                                  = 1.00   <- floor binds
-- ---------------------------------------------------------------------------
do $section_10$
declare
    v_org uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_evidence record;
begin
    select org_id, window_start, window_end into v_org, v_start, v_end
      from pg_temp.new_billing_org('section10-gross-floor');

    perform pg_temp.paid_miss(v_org, v_start + interval '1 hour',
                              'proxy:s10-ancestor', 1.00, 1000000);
    perform pg_temp.replay(v_org, v_start + interval '2 hours',
                           'proxy:s10-replay', 'proxy:s10-ancestor', 5000000, 2.00);
    -- A netting correction. 202607280008 is explicit that savings are netted
    -- across the period and NOT floored per row: offsetting rows must cancel.
    perform pg_temp.replay(v_org, v_start + interval '3 hours',
                           'proxy:s10-correction', '', 0, -1.00);

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_org, v_start, v_end);
    if v_evidence.net_verified_savings_usd <> 1.00 then
        raise exception 'SECTION 10: the fixture''s gross is %, expected 1.00',
            v_evidence.net_verified_savings_usd;
    end if;
    -- (A) binds on the replay, so the uncapped anchored component is the full
    -- 2.00 -- twice the period's gross.
    if v_evidence.anchored_zero_spend_savings_usd <> 2.00 then
        raise exception 'SECTION 10: the anchored component is %, expected 2.00; the floor '
                        'below is then untested', v_evidence.anchored_zero_spend_savings_usd;
    end if;
    if v_evidence.billable_savings_basis_usd <> 1.00 then
        raise exception 'SECTION 10: the fee basis is %, expected 1.00 -- the basis must never '
                        'exceed the period''s own gross netted savings, or the settlement row '
                        'records a number nothing can reproduce',
            v_evidence.billable_savings_basis_usd;
    end if;
end;
$section_10$;

-- Every fixture above -- organizations, billing accounts, attestations, usage
-- rows, warm ledger rows and settlements -- is discarded here. This is not
-- housekeeping: period_settlement_ledger carries an unconditional BEFORE DELETE
-- guard, so nothing written above could be cleaned up any other way.
rollback;
