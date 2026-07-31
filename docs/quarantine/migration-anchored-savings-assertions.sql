\set ON_ERROR_STOP on

-- Behavioural contract for the ANCHORED FEE BASIS
-- (202607280028_anchored_zero_spend_fee_basis.sql).
--
-- WHAT IS BEING PROVEN. Before 202607280028 a Brevitas exact-cache replay was
-- structurally unbillable: it carries actual_cost_usd = 0 by construction, so a
-- period made of replays sat at zero_spend_share 1.00000 for ever and
-- 202607280008's concentration condition halted it. After 202607280028 a
-- zero-spend saving is billable if and only if it is ANCHORED -- a
-- Brevitas-attributable, verified, proxy-minted cache replay for which a real,
-- receipted, PAID request by the same organization for the same
-- (provider, model) exists at or before it. Unanchored zero-spend savings are
-- EXCLUDED from the fee basis entirely; they can never contribute a
-- micro-dollar. The concentration guard is retained over what remains.
--
-- The numbers in section 3 are the ones actually observed on caestus org
-- f1305475-...-2191 in the 2026-07-30 customer dress rehearsal
-- (docs/STRIPE_BUILD_REPORT.md, "CUSTOMER DRESS REHEARSAL"): four paid DeepSeek
-- cache misses and four exact_cache replays summing $0.0021019600, which halted
-- with zero_spend_net_savings_usd = net_verified_savings_usd = 0.0021019600,
-- share 1.00000 and recomputed_fee_microusd = 525. This file asserts that the
-- same eight rows now SETTLE at exactly 525 microUSD, and -- section 4 -- that
-- flipping nothing but cache_attributable puts the very same rows back under
-- the old verbatim halt. That pair is the whole change, isolated.
--
-- Arithmetic used elsewhere in the file, so every expected number is checkable
-- by hand: verified savings 100.00 USD, recomputed warm spend 10.00 USD, so
-- net-after-warm is 90.00 USD and the fee is 90 * 0.25 * 1e6 = 22,500,000
-- microUSD -- the same boundary period_settlement_ledger's own fee CHECK
-- enforces from the row's recorded columns. Sections 7 and 8 re-run that cap,
-- the warm deduction and the PSL-LATCH promote-door refusal against the
-- ANCHORED path specifically, because those proofs previously only ever ran on
-- spend-backed evidence and would not have noticed if the new basis had broken
-- them.
--
-- Every window is derived from now() rather than a fixed calendar date, for the
-- same reason the settlement-writer suite does it: two of the conditions under
-- test are now()-relative. now() is transaction-stable, so this is still
-- deterministic.
--
-- The trailing rollback discards every fixture, so this file is re-runnable at
-- any point in the replay and leaves no settlement behind in what 202607280007
-- calls a seven-year financial record.

begin;

-- Same non-UTC session timezone the settlement-writer suite uses: week
-- arithmetic here is 604800 FIXED seconds and must not be wall-clock dependent.
set local timezone = 'America/New_York';

do $$
declare
    v_owner uuid := 'a0c40001-0000-4000-8000-00000000000a';

    v_dress_org uuid := 'a0c40001-0000-4000-8000-000000000001';
    v_flip_org uuid := 'a0c40001-0000-4000-8000-000000000002';
    v_basis_org uuid := 'a0c40001-0000-4000-8000-000000000003';
    v_excluded_org uuid := 'a0c40001-0000-4000-8000-000000000004';
    v_dominant_org uuid := 'a0c40001-0000-4000-8000-000000000005';
    v_native_org uuid := 'a0c40001-0000-4000-8000-000000000006';
    v_rule_org uuid := 'a0c40001-0000-4000-8000-000000000007';
    v_other_org uuid := 'a0c40001-0000-4000-8000-000000000008';
    v_cap_org uuid := 'a0c40001-0000-4000-8000-000000000009';
    v_lowered_org uuid := 'a0c40001-0000-4000-8000-00000000000b';
    v_promote_org uuid := 'a0c40001-0000-4000-8000-00000000000c';

    -- The week that closed most recently, and the week that is still open.
    v_end constant timestamptz := date_trunc('hour', now()) - interval '1 hour';
    v_start constant timestamptz := v_end - interval '604800 seconds';
    v_next_end constant timestamptz := v_end + interval '604800 seconds';
    v_warm_day constant date := ((v_start at time zone 'UTC') + interval '1 day')::date;
    v_anchor constant timestamptz := v_start + interval '1 hour';
    v_replay constant timestamptz := v_start + interval '2 hours';

    -- The dress-rehearsal evidence, to the last digit.
    v_dress_savings constant numeric := 0.0021019600;
    v_dress_per_replay constant numeric := 0.0005254900;
    v_dress_per_miss constant numeric := 0.0002000000;
    v_dress_fee constant bigint := 525;

    v_evidence record;
    v_result jsonb;
    v_row public.period_settlement_ledger%rowtype;
    v_basis_fee bigint;
    v_excluded_fee bigint;
    v_settlement_id bigint;
    v_anchor_id bigint;
    v_ancestor_id bigint;
    v_count bigint;
begin
    ------------------------------------------------------------------
    -- 0. Structure. The anchored basis is only real if the evidence function
    --    actually reports it and the writer actually reads it. Assert both
    --    before any behaviour, so a silently un-applied 202607280028 fails
    --    here instead of producing a confusing arithmetic mismatch below.
    ------------------------------------------------------------------
    if to_regprocedure('public.billing_zero_spend_savings_anchor_id'
                       '(uuid,timestamptz,text,text,boolean,text,text,text)') is null then
        raise exception 'the anchor lookup is missing (202607280028 not applied)';
    end if;
    if not exists (
        select 1
          from pg_proc evidence_function
          cross join lateral unnest(evidence_function.proargnames) as argument(name)
         where evidence_function.oid = to_regprocedure(
                   'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
           and argument.name = 'billable_savings_basis_usd'
    ) then
        raise exception 'the evidence function does not report billable_savings_basis_usd';
    end if;
    if not exists (
        select 1 from pg_proc writer
         where writer.oid = to_regprocedure(
                   'public.settle_billing_period(uuid,timestamptz,text,boolean)')
           and writer.prosrc like '%billable_savings_basis_usd%'
    ) then
        raise exception 'the settlement writer still derives its fee from the un-narrowed basis';
    end if;
    -- The anchor lookup must not be reachable from a browser role: it reads
    -- across every organization's usage_log by design.
    if has_function_privilege('anon',
           'public.billing_zero_spend_savings_anchor_id'
           '(uuid,timestamptz,text,text,boolean,text,text,text)', 'execute')
       or has_function_privilege('authenticated',
           'public.billing_zero_spend_savings_anchor_id'
           '(uuid,timestamptz,text,text,boolean,text,text,text)', 'execute') then
        raise exception 'the anchor lookup is reachable from a browser role';
    end if;
    -- 202607280008 must still be the guard, unedited, consuming the column this
    -- migration narrowed. If someone "fixed" the drought by editing the frozen
    -- condition instead, this file's whole argument is void.
    if (select procedure.prosrc
          from pg_proc procedure
         where procedure.oid = to_regprocedure(
             'public.assert_billing_period_halting_conditions'
             '(uuid,timestamptz,timestamptz,bigint)'))
       not like '%zero_spend_net_savings_usd%' then
        raise exception 'the frozen 202607280008 concentration guard is gone or rewritten';
    end if;

    ------------------------------------------------------------------
    -- 1. Fixtures. One organization per property, so no property can pass
    --    because a sibling fixture moved an aggregate.
    ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_owner, 'anchored-savings-assertion@example.invalid');

    insert into public.organizations (id, name, billing_owner_id) values
        (v_dress_org,     'Anchored: dress rehearsal',        v_owner),
        (v_flip_org,      'Anchored: attribution flipped',    v_owner),
        (v_basis_org,     'Anchored: basis baseline',         v_owner),
        (v_excluded_org,  'Anchored: unanchored excluded',    v_owner),
        (v_dominant_org,  'Anchored: unanchored dominates',   v_owner),
        (v_native_org,    'Anchored: provider-native cache',  v_owner),
        (v_rule_org,      'Anchored: anchor rule',            v_owner),
        (v_other_org,     'Anchored: other tenant',           v_owner),
        (v_cap_org,       'Anchored: 25% cap and warm',       v_owner),
        (v_lowered_org,   'Anchored: lowered ceiling',        v_owner),
        (v_promote_org,   'Anchored: promote door',           v_owner);

    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    )
    select org_id, v_owner, 'active', v_start - interval '1 day', v_end, v_next_end
      from unnest(array[
          v_dress_org, v_flip_org, v_basis_org, v_excluded_org, v_dominant_org,
          v_native_org, v_cap_org, v_lowered_org, v_promote_org
      ]) as org_id;

    -- Only a per-call marginal arrangement may be billed at all (202607280009).
    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by, attested_evidence)
    select org_id, 'marginal_per_call', 'anchored-savings assertion', 'fixture'
      from unnest(array[
          v_dress_org, v_flip_org, v_basis_org, v_excluded_org, v_dominant_org,
          v_native_org, v_cap_org, v_lowered_org, v_promote_org
      ]) as org_id;

    ------------------------------------------------------------------
    -- 2. THE DRESS-REHEARSAL SHAPE, seeded exactly.
    --
    --    Four real DeepSeek completions that cost money and saved nothing (a
    --    cache MISS pays full price and records no saving), then four
    --    exact_cache replays that cost nothing and saved $0.00052549 each.
    --    Both orgs get identical rows; v_flip_org exists so section 4 can
    --    change ONE boolean and nothing else.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-dress', 'anchored-dress', org_id,
           v_anchor + (miss.n * interval '1 minute'),
           'dress-miss-' || org_id::text || '-' || miss.n::text, 'dress',
           'deepseek', 'deepseek-chat', '', '', false,
           0, v_dress_per_miss, v_dress_per_miss,
           true, 'priced', 'proxy'
      from unnest(array[v_dress_org, v_flip_org]) as org_id,
           generate_series(1, 4) as miss(n);

    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-dress', 'anchored-dress', org_id,
           v_replay + (replay.n * interval '1 minute'),
           'dress-replay-' || org_id::text || '-' || replay.n::text, 'dress',
           'deepseek', 'deepseek-chat', 'exact_cache', 'verified', true,
           v_dress_per_replay, 0, v_dress_per_replay,
           true, 'priced', 'proxy'
      from unnest(array[v_dress_org, v_flip_org]) as org_id,
           generate_series(1, 4) as replay(n);

    ------------------------------------------------------------------
    -- 3. AN ANCHORED CACHE REPLAY'S SAVINGS ENTER THE FEE BASIS.
    --
    --    This is the assertion the whole migration exists for. The evidence
    --    numbers below are the caestus ones, verbatim.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_dress_org, v_start, v_end);

    if v_evidence.eligible_rows <> 8 then
        raise exception 'the dress-rehearsal fixture is not 8 eligible rows: %',
            v_evidence.eligible_rows;
    end if;
    if v_evidence.net_verified_savings_usd <> v_dress_savings then
        raise exception 'the dress-rehearsal gross savings drifted from the observed %: %',
            v_dress_savings, v_evidence.net_verified_savings_usd;
    end if;
    -- All four replays are zero-spend, and all four are anchored by the four
    -- paid misses that precede them for the same (provider, model).
    if v_evidence.zero_spend_rows <> 4
       or v_evidence.anchored_zero_spend_rows <> 4
       or v_evidence.unanchored_zero_spend_rows <> 0 then
        raise exception 'the anchor classification of the dress-rehearsal rows is wrong: %',
            to_jsonb(v_evidence);
    end if;
    if v_evidence.anchored_zero_spend_savings_usd <> v_dress_savings then
        raise exception 'the anchored savings are not the whole $% observed: %',
            v_dress_savings, v_evidence.anchored_zero_spend_savings_usd;
    end if;
    -- The guard's numerator is now EMPTY: nothing here is unpaid-for.
    if v_evidence.zero_spend_net_savings_usd <> 0 then
        raise exception 'anchored replays still count as savings nobody paid for: %',
            v_evidence.zero_spend_net_savings_usd;
    end if;
    -- THE FEE BASIS. Every dollar of it has a receipted paid ancestor.
    if v_evidence.billable_savings_basis_usd <> v_dress_savings then
        raise exception 'the anchored replays did not enter the fee basis: %',
            v_evidence.billable_savings_basis_usd;
    end if;
    -- The paid misses are the marginal dollar the halting conditions demanded.
    if v_evidence.actual_spend_usd <> v_dress_per_miss * 4 then
        raise exception 'the paid cache misses did not sum: %', v_evidence.actual_spend_usd;
    end if;

    -- The frozen guard now passes this period at the exact fee the rehearsal
    -- computed and refused to charge.
    v_result := public.assert_billing_period_halting_conditions(
        v_dress_org, v_start, v_end, v_dress_fee);
    if (v_result->>'zero_spend_share')::numeric <> 0 then
        raise exception 'the concentration share of an all-anchored period is not zero: %',
            v_result;
    end if;
    if (v_result->>'fee_ceiling_microusd')::bigint <> v_dress_fee then
        raise exception 'the ceiling on the observed evidence is not % microUSD: %',
            v_dress_fee, v_result;
    end if;

    -- And it settles.
    v_result := public.settle_billing_period(v_dress_org, v_anchor, 'assertion:dress');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the dress-rehearsal shape still does not settle: %', v_result;
    end if;
    if (v_result->>'fee_microusd')::bigint <> v_dress_fee then
        raise exception 'the dress-rehearsal fee is not the observed % microUSD: %',
            v_dress_fee, v_result;
    end if;
    if (v_result->>'anchored_zero_spend_rows')::bigint <> 4
       or (v_result->>'unanchored_zero_spend_savings_usd')::numeric <> 0 then
        raise exception 'the settlement body does not carry the anchoring evidence: %', v_result;
    end if;

    select * into v_row
      from public.period_settlement_ledger
     where organization_id = v_dress_org;
    -- The recorded basis IS the anchored savings, so the table's own fee CHECK
    -- re-derives 525 from the stored columns. That is what makes the exclusion
    -- structural rather than advisory.
    if v_row.verified_savings_usd <> v_dress_savings
       or v_row.warm_spend_usd <> 0
       or v_row.net_savings_usd <> v_dress_savings
       or v_row.fee_microusd <> v_dress_fee
       or v_row.status <> 'draft'
       or v_row.usage_row_count <> 8 then
        raise exception 'the dress-rehearsal settlement row is wrong: %', to_jsonb(v_row);
    end if;
    if v_row.fee_microusd <> public.period_settlement_fee_microusd(
           v_row.net_savings_usd, v_row.verified_savings_usd) then
        raise exception 'the recorded fee is not the shared formula over the recorded basis';
    end if;

    ------------------------------------------------------------------
    -- 4. THE SAME EIGHT ROWS, WITH ONLY cache_attributable FLIPPED, GO BACK
    --    UNDER THE OLD VERBATIM HALT.
    --
    --    Nothing else about v_flip_org differs from v_dress_org: same rows,
    --    same amounts, same timestamps offsets, same paid ancestors. This is
    --    the control that proves the anchor -- and not some incidental change
    --    to netting or thresholds -- is what unblocked section 3, and that a
    --    provider-native or caller-owned cache discount (which is exactly a
    --    zero-spend saving with cache_attributable = false) still cannot be
    --    billed.
    ------------------------------------------------------------------
    update public.usage_log
       set cache_attributable = false
     where organization_id = v_flip_org
       and strategy = 'exact_cache';

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_flip_org, v_start, v_end);
    if v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.unanchored_zero_spend_rows <> 4 then
        raise exception 'flipping cache_attributable did not un-anchor the replays: %',
            to_jsonb(v_evidence);
    end if;
    -- The observed caestus evidence line, to the digit:
    --   zero_spend_net_savings_usd = net_verified_savings_usd = 0.0021019600
    if v_evidence.zero_spend_net_savings_usd <> v_dress_savings
       or v_evidence.net_verified_savings_usd <> v_dress_savings then
        raise exception 'the un-anchored period does not reproduce the observed evidence: %',
            to_jsonb(v_evidence);
    end if;
    if v_evidence.billable_savings_basis_usd <> 0 then
        raise exception 'unanchored zero-spend savings reached the fee basis: %',
            v_evidence.billable_savings_basis_usd;
    end if;

    v_result := public.settle_billing_period(v_flip_org, v_anchor, 'assertion:flip');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'an all-unanchored period no longer halts on concentration: %', v_result;
    end if;
    -- share 1.00000 against the 0.50 limit, exactly as observed.
    if position('1.00000' in coalesce(v_result->>'message', '')) = 0 then
        raise exception 'the halt does not report the observed 1.00000 share: %', v_result;
    end if;
    if position('zero_spend_net_savings_usd=' || v_dress_savings::text
                in coalesce(v_result->>'detail', '')) = 0 then
        raise exception 'the halt detail does not carry the observed unpaid-for savings: %',
            v_result;
    end if;
    -- The rehearsal's `recomputed_fee_microusd=525` is what the un-narrowed
    -- basis would have charged; it is the amount the guard is evaluated
    -- against, and it is NOT what would be billed (that is now 0).
    if (v_result->>'gate_fee_microusd')::bigint <> v_dress_fee then
        raise exception 'the halt does not reproduce the observed gate fee %: %',
            v_dress_fee, v_result;
    end if;
    if (v_result->>'recomputed_fee_microusd')::bigint <> 0 then
        raise exception 'an all-unanchored period would still have billed something: %', v_result;
    end if;
    if exists (select 1 from public.period_settlement_ledger
                where organization_id = v_flip_org) then
        raise exception 'the concentration halt wrote a settlement row';
    end if;

    ------------------------------------------------------------------
    -- 5. AN UNANCHORED ZERO-SPEND ROW CHANGES NO FEE.
    --
    --    v_basis_org and v_excluded_org are identical except that
    --    v_excluded_org also carries one unanchored zero-spend saving of
    --    $30.00 -- more than the anchored replay it sits beside. If exclusion
    --    were "guarded" rather than structural, that row would raise the fee.
    --    The two settlements must agree to the micro-dollar.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-basis', 'anchored-basis', org_id, v_anchor,
           'basis-paid-' || org_id::text, 'basis',
           'deepseek', 'deepseek-chat', 'compress', 'verified', false,
           100, 40, 140, true, 'priced', 'proxy'
      from unnest(array[v_basis_org, v_excluded_org, v_dominant_org]) as org_id;
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-basis', 'anchored-basis', org_id, v_replay,
           'basis-replay-' || org_id::text, 'basis',
           'deepseek', 'deepseek-chat', 'exact_cache', 'verified', true,
           20, 0, 20, true, 'priced', 'proxy'
      from unnest(array[v_basis_org, v_excluded_org, v_dominant_org]) as org_id;
    -- The excluded row: a zero-spend saving Brevitas cannot show a receipt for.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'anchored-basis', 'anchored-basis', v_excluded_org,
        v_replay + interval '1 hour', 'basis-unanchored-excluded', 'basis',
        'deepseek', 'deepseek-chat', '', '', false,
        30, 0, 30, true, 'priced', 'proxy'
    );
    -- Same shape, but big enough to dominate what remains: 230 of 350.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('anchored-basis', 'anchored-basis', v_dominant_org,
         v_replay + interval '1 hour', 'basis-unanchored-a', 'basis',
         'deepseek', 'deepseek-chat', '', '', false,
         30, 0, 30, true, 'priced', 'proxy'),
        ('anchored-basis', 'anchored-basis', v_dominant_org,
         v_replay + interval '2 hours', 'basis-unanchored-b', 'basis',
         'deepseek', 'deepseek-chat', '', '', false,
         200, 0, 200, true, 'priced', 'proxy');

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_basis_org, v_start, v_end);
    if v_evidence.net_verified_savings_usd <> 120
       or v_evidence.billable_savings_basis_usd <> 120
       or v_evidence.anchored_zero_spend_savings_usd <> 20
       or v_evidence.zero_spend_net_savings_usd <> 0 then
        raise exception 'the baseline anchored period is not 120 billable: %', to_jsonb(v_evidence);
    end if;

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_excluded_org, v_start, v_end);
    -- Gross rises by the excluded 30; the BASIS does not move at all.
    if v_evidence.net_verified_savings_usd <> 150 then
        raise exception 'the unanchored row did not reach the gross evidence: %',
            to_jsonb(v_evidence);
    end if;
    if v_evidence.billable_savings_basis_usd <> 120 then
        raise exception 'an unanchored zero-spend saving entered the fee basis: %',
            v_evidence.billable_savings_basis_usd;
    end if;
    if v_evidence.zero_spend_net_savings_usd <> 30
       or v_evidence.unanchored_zero_spend_rows <> 1
       or v_evidence.anchored_zero_spend_rows <> 1 then
        raise exception 'the unanchored residue is mis-classified: %', to_jsonb(v_evidence);
    end if;

    v_result := public.settle_billing_period(v_basis_org, v_anchor, 'assertion:basis');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the baseline anchored period did not settle: %', v_result;
    end if;
    v_basis_fee := (v_result->>'fee_microusd')::bigint;
    if v_basis_fee <> 30000000 then
        raise exception 'the baseline anchored fee is not 25%% of 120 USD: %', v_result;
    end if;

    v_result := public.settle_billing_period(v_excluded_org, v_anchor, 'assertion:excluded');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the period carrying an unanchored row did not settle: %', v_result;
    end if;
    v_excluded_fee := (v_result->>'fee_microusd')::bigint;
    if v_excluded_fee <> v_basis_fee then
        raise exception 'an unanchored zero-spend saving moved the fee: % vs %',
            v_excluded_fee, v_basis_fee;
    end if;
    -- 25% of the GROSS 150 would have been 37,500,000. Name the number that
    -- must never appear, so a regression cannot pass by coincidence.
    if v_excluded_fee = 37500000 then
        raise exception 'the fee was charged against the un-narrowed 150 USD gross';
    end if;
    select * into v_row
      from public.period_settlement_ledger where organization_id = v_excluded_org;
    if v_row.verified_savings_usd <> 120 then
        raise exception 'the ledger recorded the gross rather than the anchored basis: %',
            to_jsonb(v_row);
    end if;

    ------------------------------------------------------------------
    -- 6. ...AND UNANCHORED SAVINGS STILL TRIP THE CONCENTRATION GUARD WHEN
    --    THEY DOMINATE WHAT REMAINS.
    --
    --    230 unpaid-for of 350 gross is 0.65714, above the 0.50 limit. The
    --    original fraud tripwire is retained in full: excluding these savings
    --    from the fee basis is not the same as forgiving the period.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_dominant_org, v_start, v_end);
    if v_evidence.net_verified_savings_usd <> 350
       or v_evidence.zero_spend_net_savings_usd <> 230
       or v_evidence.billable_savings_basis_usd <> 120 then
        raise exception 'the dominating-residue fixture is not 230 of 350: %',
            to_jsonb(v_evidence);
    end if;

    v_result := public.settle_billing_period(v_dominant_org, v_anchor, 'assertion:dominant');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'zero_spend_concentration' then
        raise exception 'unanchored savings that dominate no longer halt the period: %',
            v_result;
    end if;
    if exists (select 1 from public.period_settlement_ledger
                where organization_id = v_dominant_org) then
        raise exception 'the retained concentration guard still wrote a row';
    end if;
    -- The guard sees the residue, not the anchored replay beside it.
    v_result := public.assert_billing_period_halting_conditions(
        v_dominant_org, v_start, v_end, 0::bigint);
    if round((v_result->>'zero_spend_share')::numeric, 5) <> 0.65714 then
        raise exception 'the retained share is not 230/350: %', v_result;
    end if;

    ------------------------------------------------------------------
    -- 7. PROVIDER-NATIVE CACHE DISCOUNTS CONTRIBUTE SPEND AND NO SAVINGS.
    --
    --    A DeepSeek prompt_cache_hit_tokens row is cache_attributable = false
    --    and spend-backed: Brevitas did not cause the discount, so
    --    brevitas/receipts.py attributes no saving to it. It must raise
    --    actual_spend_usd -- it is a real marginal dollar -- and must raise
    --    neither the gross savings nor the fee basis. And the anchor lookup
    --    must refuse it on cache_attributable alone, even though a qualifying
    --    paid ancestor exists for its (provider, model).
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        cached_input_tokens, fresh_input_tokens,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        -- A full-price paid call: the receipted ancestor.
        ('anchored-native', 'anchored-native', v_native_org, v_anchor,
         'native-paid', 'native', 'deepseek', 'deepseek-chat', '', '', false,
         0, 5000, 0, 10, 10, true, 'priced', 'proxy'),
        -- The provider's own prompt-cache discount. Cheaper, still paid, and
        -- worth nothing to us.
        ('anchored-native', 'anchored-native', v_native_org,
         v_anchor + interval '10 minutes',
         'native-discounted', 'native', 'deepseek', 'deepseek-chat', '', '', false,
         4800, 200, 0, 4, 4, true, 'priced', 'proxy');

    select * into v_evidence
      from public.billing_period_settlement_evidence(v_native_org, v_start, v_end);
    if v_evidence.actual_spend_usd <> 14 then
        raise exception 'the provider-native rows did not contribute their spend: %',
            v_evidence.actual_spend_usd;
    end if;
    if v_evidence.net_verified_savings_usd <> 0
       or v_evidence.billable_savings_basis_usd <> 0
       or v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.zero_spend_rows <> 0 then
        raise exception 'a provider-native cache discount produced billable savings: %',
            to_jsonb(v_evidence);
    end if;
    v_result := public.settle_billing_period(v_native_org, v_anchor, 'assertion:native');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 0 then
        raise exception 'a provider-native-discount period did not settle at zero: %', v_result;
    end if;

    -- The differential: one boolean is the entire difference between a
    -- provider-native discount and a Brevitas replay. Take the discounted row's
    -- own tuple and probe the anchor lookup both ways.
    select id into v_ancestor_id
      from public.usage_log
     where organization_id = v_native_org and request_id = 'native-paid';
    if public.billing_zero_spend_savings_anchor_id(
           v_native_org, v_anchor, 'deepseek', 'deepseek-chat',
           false, 'exact_cache', 'verified', 'proxy') is not null then
        raise exception 'a cache_attributable=false row was anchored';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           v_native_org, v_anchor, 'deepseek', 'deepseek-chat',
           true, 'exact_cache', 'verified', 'proxy') is distinct from v_ancestor_id then
        raise exception 'the same tuple with cache_attributable=true did not anchor to %',
            v_ancestor_id;
    end if;

    ------------------------------------------------------------------
    -- 8. THE ANCESTOR RULE, one near miss at a time.
    --
    --    Each row below fails exactly one condition of "a real, receipted, PAID
    --    request by the same organization for the same (provider, model), at or
    --    before the replay". None of them may anchor anything. Then one
    --    qualifying ancestor is added and the same probe must find it -- so a
    --    predicate that simply always returned NULL could not pass this file.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        -- Right shape, wrong tenant.
        ('anchored-rule', 'anchored-rule', v_other_org, v_anchor,
         'rule-other-tenant', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 9, 9, true, 'priced', 'proxy'),
        -- Right tenant, different model.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
         'rule-other-model', 'rule', 'deepseek', 'deepseek-reasoner', '', '', false,
         0, 9, 9, true, 'priced', 'proxy'),
        -- Caller-reported: never billing input, never evidence of a payment.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
         'rule-nonauthoritative', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 9, 9, false, 'priced', 'proxy'),
        -- Never priced: its cost was never resolved.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
         'rule-unpriced', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 9, 9, true, 'unpriced', 'proxy'),
        -- No provider receipt through us.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
         'rule-sdk-receipt', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 9, 9, true, 'priced', 'sdk'),
        -- Free: no money left the customer's account.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
         'rule-free', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 0, 0, true, 'priced', 'proxy'),
        -- Paid, but AFTER the replay: a cache entry cannot replay the future.
        ('anchored-rule', 'anchored-rule', v_rule_org, v_replay + interval '1 hour',
         'rule-later', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
         0, 9, 9, true, 'priced', 'proxy');

    if public.billing_zero_spend_savings_anchor_id(
           v_rule_org, v_replay, 'deepseek', 'deepseek-chat',
           true, 'exact_cache', 'verified', 'proxy') is not null then
        raise exception 'a replay was anchored with no qualifying paid ancestor';
    end if;

    -- Now the real thing.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'anchored-rule', 'anchored-rule', v_rule_org, v_anchor,
        'rule-qualifying', 'rule', 'deepseek', 'deepseek-chat', '', '', false,
        0, 9, 9, true, 'priced', 'proxy'
    );
    select id into v_ancestor_id
      from public.usage_log
     where organization_id = v_rule_org and request_id = 'rule-qualifying';
    v_anchor_id := public.billing_zero_spend_savings_anchor_id(
        v_rule_org, v_replay, 'deepseek', 'deepseek-chat',
        true, 'exact_cache', 'verified', 'proxy');
    if v_anchor_id is distinct from v_ancestor_id then
        raise exception 'the qualifying ancestor % was not found (got %)',
            v_ancestor_id, v_anchor_id;
    end if;
    -- A semantic replay that was proven byte-preserving anchors the same way;
    -- an unverified one, or a non-replay strategy, does not.
    if public.billing_zero_spend_savings_anchor_id(
           v_rule_org, v_replay, 'deepseek', 'deepseek-chat',
           true, 'semantic_cache', 'verified', 'proxy') is distinct from v_ancestor_id then
        raise exception 'a verified semantic replay did not anchor';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           v_rule_org, v_replay, 'deepseek', 'deepseek-chat',
           true, 'exact_cache', 'unverified', 'proxy') is not null then
        raise exception 'an unverified replay was anchored';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           v_rule_org, v_replay, 'deepseek', 'deepseek-chat',
           true, 'compress', 'verified', 'proxy') is not null then
        raise exception 'a non-replay strategy was anchored';
    end if;

    ------------------------------------------------------------------
    -- 9. THE 25% CAP AND THE WARM DEDUCTION, ON THE ANCHORED PATH.
    --
    --    These were proven in migration-settlement-writer-assertions.sql over
    --    spend-backed evidence only. Re-run the key arithmetic where the whole
    --    saving is a zero-spend replay: verified 100, warm 10, net 90, fee
    --    22,500,000 microUSD -- and the warm deduction must come off the
    --    ANCHORED basis, not off the gross.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-cap', 'anchored-cap', org_id, v_anchor,
           'cap-paid-' || org_id::text, 'cap',
           'deepseek', 'deepseek-chat', '', '', false,
           0, 40, 40, true, 'priced', 'proxy'
      from unnest(array[v_cap_org, v_lowered_org, v_promote_org]) as org_id;
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'anchored-cap', 'anchored-cap', org_id, v_replay,
           'cap-replay-' || org_id::text, 'cap',
           'deepseek', 'deepseek-chat', 'exact_cache', 'verified', true,
           100, 0, 100, true, 'priced', 'proxy'
      from unnest(array[v_cap_org, v_lowered_org, v_promote_org]) as org_id;
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    select org_id, 'anthropic', v_warm_day, 10
      from unnest(array[v_cap_org, v_lowered_org, v_promote_org]) as org_id;

    v_result := public.settle_billing_period(v_cap_org, v_anchor, 'assertion:cap');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 22500000 then
        raise exception 'the 25%% cap after the warm deduction moved on the anchored path: %',
            v_result;
    end if;
    select * into v_row
      from public.period_settlement_ledger where organization_id = v_cap_org;
    if v_row.verified_savings_usd <> 100
       or v_row.warm_spend_usd <> 10
       or v_row.net_savings_usd <> 90 then
        raise exception 'the warm deduction was not recomputed onto the anchored row: %',
            to_jsonb(v_row);
    end if;
    if v_row.fee_microusd > floor(least(
           greatest(v_row.verified_savings_usd - v_row.warm_spend_usd, 0) * 0.25,
           greatest(v_row.verified_savings_usd, 0) * 0.25) * 1000000) then
        raise exception 'the anchored row breaches its own fee CHECK';
    end if;

    -- A LOWERED ceiling must lower the fee rather than halt the sweep: the
    -- writer takes least(shared formula, ceiling) and the ceiling is now
    -- computed over the anchored basis.
    update public.billing_halting_conditions
       set max_fee_share_of_verified_savings = 0.10
     where singleton;
    v_result := public.settle_billing_period(v_lowered_org, v_anchor, 'assertion:lowered');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 9000000 then
        raise exception 'a lowered ceiling did not lower the anchored fee to 90 * 0.10: %',
            v_result;
    end if;
    update public.billing_halting_conditions
       set max_fee_share_of_verified_savings = 0.25
     where singleton;

    ------------------------------------------------------------------
    -- 10. PSL-LATCH: THE PROMOTE DOOR, ON THE ANCHORED PATH.
    --
    --     A queued ('pending') settlement is already money. Settling, promoting
    --     and then re-settling with a late receipt must REFUSE, and the period
    --     must keep exactly one queued row. This is the defect that billed one
    --     period twice; it must stay closed now that the fee comes from a
    --     narrower basis and the guard is called with a different amount than
    --     the one written on the row.
    ------------------------------------------------------------------
    v_result := public.settle_billing_period(v_promote_org, v_anchor, 'assertion:promote');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 22500000 then
        raise exception 'the promote-door fixture did not settle at 22,500,000: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;

    -- The promoter re-runs the frozen guard for the amount actually about to be
    -- billed. Because the writer clamps the fee to the gross ceiling as well as
    -- to the basis ceiling, that re-run can never reject a draft this writer
    -- produced.
    v_result := public.promote_billing_period_settlement(
        v_settlement_id, 'assertion', 'anchored promote door');
    if v_result->>'outcome' <> 'promoted' then
        raise exception 'the anchored draft could not pass the promote-time guard: %', v_result;
    end if;

    -- A late anchored replay doubles the period's savings.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'anchored-cap', 'anchored-cap', v_promote_org, v_start + interval '2 days',
        'cap-late-replay', 'cap', 'deepseek', 'deepseek-chat',
        'exact_cache', 'verified', true, 100, 0, 100, true, 'priced', 'proxy'
    );

    v_result := public.settle_billing_period(
        v_promote_org, v_anchor, 'assertion:promote-late', p_allow_revision => true);
    if v_result->>'outcome' <> 'blocked'
       or v_result->>'code' <> 'period_already_committed' then
        raise exception 'the writer revised a period that was already queued: %', v_result;
    end if;
    select count(*) into v_count
      from public.period_settlement_ledger
     where organization_id = v_promote_org
       and status in ('pending', 'sending', 'reported');
    if v_count <> 1 then
        raise exception 'the promote door left % queued settlements for one period', v_count;
    end if;

    ------------------------------------------------------------------
    -- 11. THE CUSTOMER-FACING PROJECTION CONVERGES ON THE ANCHORED FEE.
    --
    --     202607280013 promises that estimated_fee_microusd "uses the same
    --     arithmetic the writer will use, so it converges exactly to the
    --     settled fee when the week closes". If the projection had been left on
    --     the gross basis, an org with unanchored savings would be shown a
    --     running estimate HIGHER than anything it can ever be charged. The
    --     LIVE week here is [v_end, v_next_end): seed it with the same shape as
    --     v_excluded_org -- 120 billable, 30 unanchored -- and require the
    --     screen to say 30,000,000, never the gross 37,500,000.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('anchored-live', 'anchored-live', v_native_org, v_end + interval '1 minute',
         'live-paid', 'live', 'openai', 'gpt-x', 'compress', 'verified', false,
         100, 40, 140, true, 'priced', 'proxy'),
        ('anchored-live', 'anchored-live', v_native_org, v_end + interval '2 minutes',
         'live-replay', 'live', 'openai', 'gpt-x', 'exact_cache', 'verified', true,
         20, 0, 20, true, 'priced', 'proxy'),
        ('anchored-live', 'anchored-live', v_native_org, v_end + interval '3 minutes',
         'live-unanchored', 'live', 'openai', 'gpt-x', '', '', false,
         30, 0, 30, true, 'priced', 'proxy');

    v_result := public.billing_period_settlement_summary(v_native_org, v_end);
    if not (v_result->>'ok')::boolean then
        raise exception 'the status summary refused a healthy account: %', v_result;
    end if;
    if (v_result->>'estimated_fee_microusd')::bigint <> 30000000 then
        raise exception 'the live-week projection is not the anchored basis: %', v_result;
    end if;
    if (v_result->>'estimated_fee_microusd')::bigint = 37500000 then
        raise exception 'the customer screen projects a fee against the un-narrowed gross';
    end if;
    if (v_result->'evidence'->>'billable_savings_basis_usd')::numeric <> 120
       or (v_result->'evidence'->>'unanchored_zero_spend_savings_usd')::numeric <> 30
       or (v_result->'evidence'->>'net_verified_savings_usd')::numeric <> 150 then
        raise exception 'the status summary cannot explain the excluded savings: %', v_result;
    end if;
    if (v_result->'evidence'->>'fee_ceiling_microusd')::bigint <> 30000000 then
        raise exception 'the screen shows a cap above anything we can charge: %', v_result;
    end if;

    ------------------------------------------------------------------
    -- 12. THE BASIS CAN NEVER EXCEED THE PERIOD'S GROSS SAVINGS.
    --
    --     Exclusions are normally subtractive, but a zero-spend row carrying
    --     NEGATIVE verified savings would be excluded too and would push the
    --     basis ABOVE the gross -- i.e. we would bill on more than the period
    --     saved. The evidence function floors the basis at the gross; prove it
    --     with the pathological row rather than trusting the reading.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        provider, model, strategy, quality_status, cache_attributable,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'anchored-basis', 'anchored-basis', v_basis_org, v_replay + interval '3 hours',
        'basis-negative-unanchored', 'basis', 'deepseek', 'deepseek-chat', '', '', false,
        -60, 0, -60, true, 'priced', 'proxy'
    );
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_basis_org, v_start, v_end);
    if v_evidence.net_verified_savings_usd <> 60 then
        raise exception 'the negative-row fixture did not net to 60: %', to_jsonb(v_evidence);
    end if;
    if v_evidence.billable_savings_basis_usd <> 60 then
        raise exception 'the fee basis exceeded the period''s gross savings: % > %',
            v_evidence.billable_savings_basis_usd, v_evidence.net_verified_savings_usd;
    end if;
end;
$$;

rollback;
