-- Phase 0 warming instrumentation (202608100001): the (org, prefix) control arm.
--
-- NOT YET REGISTERED in scripts/ci/run-migration-tests.sh, because
-- 202608100001 itself is not in scripts/ci/migration-fresh-manifest.txt --
-- registering it is blocked behind 202608010001_openrouter_reported_cost.sql,
-- which was landed without manifest entries and has no begin;/commit; so it
-- fails verifyAtomicForwardMigrations. Run by hand against a database with the
-- full chain applied until that is resolved:
--   psql "$DATABASE_URL" --set ON_ERROR_STOP=1 -f this file
--
-- Everything runs inside one transaction and rolls back.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000e0001';
    v_org uuid := '00000000-0000-4000-8000-0000000f0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000e0003';
    v_other_cust uuid := '00000000-0000-4000-8000-0000000e0004';
    v_hash text := repeat('c1', 32);
    v_row record;
    v_prefix record;
    v_claim jsonb;
    v_count integer;
    v_bucket bigint;
    v_raised boolean := false;
    v_request uuid := '00000000-0000-4000-8000-0000000e0005';
begin
    -- The signature is replaced, never overloaded: two candidates make every
    -- existing ten-argument call ambiguous, and PostgREST would 300.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)') is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb)') is not null then
        raise exception 'warm_due_claim must be the eleven-argument holdout form only';
    end if;
    if has_function_privilege('anon',
        'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)', 'execute')
       or has_function_privilege('authenticated',
        'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)', 'execute')
       or not has_function_privilege('service_role',
        'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)', 'execute') then
        raise exception 'warm_due_claim privileges are wrong after the signature change';
    end if;

    -- CROSS-LANGUAGE PARITY. The literal is api/store.py:warm_holdout_bucket
    -- evaluated on these exact inputs. If either side of the mirror is
    -- rewritten, one (org, prefix, day) unit lands in both arms at once, which
    -- is worse than having no control arm at all.
    if (get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char(date '2026-08-09', 'YYYY-MM-DD'), 'UTF8')), 0)::bigint * 16777216
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char(date '2026-08-09', 'YYYY-MM-DD'), 'UTF8')), 1) * 65536
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char(date '2026-08-09', 'YYYY-MM-DD'), 'UTF8')), 2) * 256
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char(date '2026-08-09', 'YYYY-MM-DD'), 'UTF8')), 3)
       ) <> 1885274667 then
        raise exception 'the Postgres holdout bucket disagrees with api/store.py';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'warm-holdout-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm holdout fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-holdout-1'), (v_other_cust, v_org, 'warm-holdout-2');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5');

    -- Out-of-range shares are rejected; null and 0 are the off state and are
    -- not an error, so a caller that predates the arm cannot fail a claim.
    begin
        perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900,
                                      null, 1.5);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a holdout share above 1 must be rejected';
    end if;

    -- ------------------------------------------------------------------
    -- OFF: byte-identical to 202608090001's loop.
    -- ------------------------------------------------------------------
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.prefix_hash = v_hash;
    select value into v_claim from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0) as t(value) limit 1;
    if coalesce(v_claim ->> 'status', '') <> 'claimed' then
        raise exception 'a zero share must claim exactly as before: %', v_claim;
    end if;
    select * into v_row from public.warm_decision_log;
    if v_row.decision <> 'pinged' or v_row.propensity is not null then
        raise exception 'with no randomization there is no propensity: %',
            to_jsonb(v_row);
    end if;
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash, (now() at time zone 'utc')::date,
        (v_claim ->> 'reserved_usd')::numeric, 0.30, 'warmed', 300, 60,
        (v_claim ->> 'claim_token')::uuid);
    delete from public.warm_decision_log;
    delete from public.warm_budget_ledger;

    -- ------------------------------------------------------------------
    -- ON: the boundary is strict, and it is a function of (org, prefix, day).
    -- ------------------------------------------------------------------
    v_bucket := get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char((clock_timestamp() at time zone 'utc')::date,
                       'YYYY-MM-DD'), 'UTF8')), 0)::bigint * 16777216
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char((clock_timestamp() at time zone 'utc')::date,
                       'YYYY-MM-DD'), 'UTF8')), 1) * 65536
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char((clock_timestamp() at time zone 'utc')::date,
                       'YYYY-MM-DD'), 'UTF8')), 2) * 256
        + get_byte(sha256(convert_to(
            lower(v_org::text) || lower(v_hash)
            || to_char((clock_timestamp() at time zone 'utc')::date,
                       'YYYY-MM-DD'), 'UTF8')), 3);

    -- Exactly at the unit's own coordinate the comparison is strict, so it
    -- stays in the treatment arm -- and records the treatment propensity.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           warm_pings = 0, consecutive_misses = 0, pings_today = 0,
           pings_today_date = null, claim_token = null
     where prefix.prefix_hash = v_hash;
    select value into v_claim from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null,
        v_bucket::double precision / 4294967296) as t(value) limit 1;
    if coalesce(v_claim ->> 'status', '') <> 'claimed' then
        raise exception 'the holdout threshold must be strict: %', v_claim;
    end if;
    select * into v_row from public.warm_decision_log;
    if v_row.decision <> 'pinged'
       or abs(v_row.propensity
              - (1 - v_bucket::double precision / 4294967296)::numeric) > 1e-9 then
        raise exception 'the treatment arm must record 1 - fraction: %',
            to_jsonb(v_row);
    end if;
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash, (now() at time zone 'utc')::date,
        (v_claim ->> 'reserved_usd')::numeric, 0.30, 'warmed', 300, 60,
        (v_claim ->> 'claim_token')::uuid);
    delete from public.warm_decision_log;
    delete from public.warm_budget_ledger;

    -- One step past it, the same unit is held out. Nothing is claimed, nothing
    -- is reserved, no counter moves, and only the schedule advances -- by the
    -- horizon warm_ping_settle would have applied (300 - 60).
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           warm_pings = 0, warm_hits = 0, warm_misses = 0,
           consecutive_misses = 0, pings_today = 0, pings_today_date = null,
           claim_token = null, last_touch_at = null
     where prefix.prefix_hash = v_hash;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null,
        (v_bucket + 1)::double precision / 4294967296) as t(value);
    if v_count <> 0 then
        raise exception 'a held-out prefix must not be claimed';
    end if;
    select * into v_row from public.warm_decision_log;
    if v_row.decision <> 'holdout'
       or abs(v_row.propensity
              - ((v_bucket + 1)::double precision / 4294967296)::numeric) > 1e-9
       or v_row.claim_token is not null
       or v_row.settle_outcome is not null
       or v_row.pings_today <> 0
       or v_row.reserve_usd <> 0.375 then
        raise exception 'the holdout decision is wrong: %', to_jsonb(v_row);
    end if;
    select * into v_prefix from public.warm_prefixes
     where prefix_hash = v_hash and customer_id = v_cust;
    if v_prefix.warm_pings <> 0 or v_prefix.consecutive_misses <> 0
       or v_prefix.pings_today <> 0 or v_prefix.claim_token is not null
       or v_prefix.last_touch_at is not null or v_prefix.state <> 'active' then
        raise exception 'a holdout must move no counter and take no claim: %',
            to_jsonb(v_prefix);
    end if;
    if abs(extract(epoch from (v_prefix.next_due_at - clock_timestamp())) - 240) > 10 then
        raise exception 'a holdout must advance next_due_at by the TTL horizon: %',
            v_prefix.next_due_at;
    end if;
    -- The ledger row exists because the budget gate reads it, but no money
    -- moved: a withheld ping is the one exit that spends nothing.
    select * into v_row from public.warm_budget_ledger
     where organization_id = v_org and provider = 'anthropic';
    if coalesce(v_row.reserved_usd, 0) <> 0 or coalesce(v_row.spent_usd, 0) <> 0 then
        raise exception 'a holdout must reserve and spend nothing: %', to_jsonb(v_row);
    end if;

    -- ------------------------------------------------------------------
    -- The unit is (org, prefix): every customer sharing the prefix is on the
    -- same side of the coin, because the provider cache is org-scoped.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log;
    perform public.warm_prefix_observe(
        v_org, v_other_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5');
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.prefix_hash = v_hash;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null,
        (v_bucket + 1)::double precision / 4294967296) as t(value);
    if v_count <> 0 then
        raise exception 'a sibling customer must not keep a held-out prefix warm';
    end if;
    select count(*) into v_count from public.warm_decision_log
     where decision = 'holdout';
    if v_count <> 2 then
        raise exception 'both customers on the prefix must be held out, got %', v_count;
    end if;

    -- ------------------------------------------------------------------
    -- The draw is the LAST gate. A row the budget already denied must stay
    -- 'budget_denied', or the control arm fills with rows the treatment arm
    -- would never have warmed and the comparison stops being causal.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log;
    delete from public.warm_budget_ledger;
    update public.warm_credentials cred set daily_budget_usd = 0.01
     where cred.organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.prefix_hash = v_hash;
    perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900,
                                  null, 1.0);
    if exists (select 1 from public.warm_decision_log where decision <> 'budget_denied') then
        raise exception 'the holdout draw must come after every spend gate';
    end if;
    update public.warm_credentials cred set daily_budget_usd = 10.0
     where cred.organization_id = v_org;

    -- ------------------------------------------------------------------
    -- Compliance: a holdout row is an ordinary warm_decision_log row, so it
    -- inherits 202608090001's export and 202608090002's subject erasure -- both
    -- already asserted by those suites. What is new is that the assignment
    -- record travels with it: propensity is the org's own evidence of what
    -- share was withheld on the day, and an export that drops it is an export
    -- the org cannot audit the arm from.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.prefix_hash = v_hash;
    perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900,
                                  null, 1.0);
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_request, v_org, 'export', 'customer', v_cust, 'processing',
        'evidence:warm-holdout:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-holdout',
        'system:warm-holdout', now());
    select count(*) into v_count
      from public.compliance_export_subject(v_org, v_request, 'brevitas_admin:assertion')
        as exported(record)
     where exported.record ->> 'record_type' = 'warming_decision'
       and exported.record -> 'data' ->> 'decision' = 'holdout'
       and (exported.record -> 'data' ->> 'propensity')::numeric = 1.0;
    if v_count = 0 then
        raise exception 'the subject export must carry the holdout assignment';
    end if;

    raise notice '202608100001 holdout assertions passed';
end;
$$;

rollback;
