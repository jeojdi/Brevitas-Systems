-- Phase 1 learned warming (202608100004): per-customer monthly spend envelopes,
-- the allocator, the erasure tombstone and the retention class.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000f0001';
    v_org uuid := '00000000-0000-4000-8000-0000000f0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000f0003';
    v_peer uuid := '00000000-0000-4000-8000-0000000f0004';
    v_hash_a text := repeat('f1', 32);
    v_hash_b text := repeat('f2', 32);
    v_request uuid := '00000000-0000-4000-8000-0000000f0005';
    v_tenant_request uuid := '00000000-0000-4000-8000-0000000f0006';
    v_export_request uuid := '00000000-0000-4000-8000-0000000f0008';
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean)';
    v_settle_signature text := 'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)';
    v_period date := date_trunc('month', ((clock_timestamp() at time zone 'utc')::date)::timestamp)::date;
    v_bucket text;
    v_column text;
    v_routine text;
    v_count integer;
    v_reserve numeric;
    v_claim jsonb;
    v_row public.warm_customer_budget%rowtype;
    v_peer_row public.warm_customer_budget%rowtype;
    v_ledger public.warm_budget_ledger%rowtype;
    v_ref text;
    v_raised boolean;
    v_run uuid := '00000000-0000-4000-8000-0000000f0007';
    v_result jsonb;
begin
    -- ------------------------------------------------------------------
    -- 0. Signatures. 202608100004 added NO argument to warm_due_claim -- the
    -- envelope is a gate, not an argument, because a spend ceiling a caller
    -- can switch off is not a ceiling -- and left warm_ping_settle's eleven
    -- alone. The sixteenth argument here is 202608100005's p_beta and the
    -- seventeenth is 202608100007's p_parent_dedup; what this file pins is
    -- that the fifteen-argument form is gone, so nothing can still be calling
    -- the pre-envelope claim.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is not null
       or to_regprocedure(v_settle_signature) is null then
        raise exception 'the claim/settle signatures must be unchanged by 202608100004';
    end if;
    if has_function_privilege('anon', v_claim_signature, 'execute')
       or has_function_privilege('authenticated', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_claim_signature, 'execute')
       or has_function_privilege('anon', v_settle_signature, 'execute')
       or has_function_privilege('authenticated', v_settle_signature, 'execute')
       or not has_function_privilege('service_role', v_settle_signature, 'execute') then
        raise exception 'the claim/settle privileges are wrong after 202608100004';
    end if;
    foreach v_routine in array array[
        'public.warm_customer_budget_list(uuid,text,date)',
        'public.warm_customer_budget_put(uuid,text,uuid,date,numeric)',
        'public.warm_customer_budget_allocate(integer)'
    ] loop
        if to_regprocedure(v_routine) is null then
            raise exception 'the envelope RPC % is missing', v_routine;
        end if;
        if has_function_privilege('anon', v_routine, 'execute')
           or has_function_privilege('authenticated', v_routine, 'execute')
           or not has_function_privilege('service_role', v_routine, 'execute') then
            raise exception 'the envelope RPC % has the wrong privileges', v_routine;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 1. The table. RLS on, zero policies, zero direct DML for every
    -- PostgREST role: dollars are reachable only through definer routines.
    -- ------------------------------------------------------------------
    if to_regclass('public.warm_customer_budget') is null then
        raise exception 'public.warm_customer_budget is missing';
    end if;
    if not (select relrowsecurity from pg_catalog.pg_class
             where oid = 'public.warm_customer_budget'::regclass) then
        raise exception 'public.warm_customer_budget must have row level security enabled';
    end if;
    if exists (select 1 from pg_catalog.pg_policy
                where polrelid = 'public.warm_customer_budget'::regclass) then
        raise exception 'public.warm_customer_budget must carry zero policies';
    end if;
    foreach v_column in array array['anon', 'authenticated', 'service_role'] loop
        if has_table_privilege(v_column, 'public.warm_customer_budget', 'select')
           or has_table_privilege(v_column, 'public.warm_customer_budget', 'insert')
           or has_table_privilege(v_column, 'public.warm_customer_budget', 'update')
           or has_table_privilege(v_column, 'public.warm_customer_budget', 'delete') then
            raise exception 'public.warm_customer_budget must grant % nothing', v_column;
        end if;
    end loop;
    -- No foreign key to public.customers, deliberately: a tombstoned row must
    -- outlive the customer it used to name.
    if exists (
        select 1 from pg_catalog.pg_constraint
         where conrelid = 'public.warm_customer_budget'::regclass
           and contype = 'f'
    ) then
        raise exception 'public.warm_customer_budget must carry no foreign key';
    end if;
    foreach v_column in array array[
        'warm_customer_budget_candidates', 'warm_customer_budget_deleted'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute attribute
             where attribute.attrelid = 'public.compliance_retention_runs'::regclass
               and attribute.attname = v_column
               and not attribute.attisdropped
        ) then
            raise exception 'compliance_retention_runs.% is missing', v_column;
        end if;
        if not exists (
            select 1 from pg_catalog.pg_constraint
             where conrelid = 'public.compliance_retention_runs'::regclass
               and conname = 'compliance_retention_runs_' || v_column || '_check'
        ) then
            raise exception 'compliance_retention_runs.% has no bound', v_column;
        end if;
    end loop;

    insert into auth.users (id, email)
    values (v_actor, 'warm-envelope-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm envelope fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-envelope-subject'),
           (v_peer, v_org, 'warm-envelope-peer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_b, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    v_bucket := ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20)
     where prefix.organization_id = v_org;
    select prefix.ping_reserve_usd into v_reserve
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_cust;
    if coalesce(v_reserve, 0) <= 0 then
        raise exception 'the fixture prefix has no observer-priced reservation';
    end if;

    -- ------------------------------------------------------------------
    -- 2. No row means NO CONSTRAINT. This is the default-off story for a gate
    -- that cannot take a boolean argument, so it is asserted rather than
    -- argued: with an empty table the claim loop is 202608100003's.
    -- ------------------------------------------------------------------
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false) as t(value);
    if v_count <> 2 then
        raise exception 'an empty envelope table must constrain nothing, claimed %', v_count;
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.decision <> 'pinged'
    ) then
        raise exception 'an empty envelope table must deny nothing';
    end if;
    if exists (select 1 from public.warm_customer_budget
                where organization_id = v_org) then
        raise exception 'the claim path must not create envelope rows';
    end if;

    -- ------------------------------------------------------------------
    -- 3. The gate denies INSIDE warm_due_claim, and a denial is the whole
    -- effect: no reservation on either book, no claim token.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null
     where prefix.organization_id = v_org;
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd, source)
    values (v_org, 'anthropic', v_period, v_cust::text,
            round(v_reserve / 2, 10), 'org_override'),
           (v_org, 'anthropic', v_period, v_peer::text,
            round(v_reserve * 10, 10), 'auto');

    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false) as t(value);
    if v_count <> 1 then
        raise exception 'exactly the peer must claim under a binding envelope, claimed %', v_count;
    end if;
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_cust
           and entry.decision = 'envelope_denied'
    ) then
        raise exception 'the capped customer must be logged as envelope_denied';
    end if;
    select * into v_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    if v_row.reserved_usd <> 0 or v_row.spent_usd <> 0 then
        raise exception 'a denial must move no envelope money: %', to_jsonb(v_row);
    end if;
    if exists (
        select 1 from public.warm_prefixes prefix
         where prefix.organization_id = v_org and prefix.customer_id = v_cust
           and prefix.claim_token is not null
    ) then
        raise exception 'a denial must take no claim token';
    end if;
    -- The admitted peer moved BOTH books by the same reservation.
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    select * into v_ledger from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_peer_row.reserved_usd <> v_ledger.reserved_usd
       or v_peer_row.reserved_usd <= 0 then
        raise exception 'the envelope and the ledger must reserve the same dollars: % / %',
            to_jsonb(v_peer_row), to_jsonb(v_ledger);
    end if;

    -- ------------------------------------------------------------------
    -- 4. Settle moves both books for every outcome, with the identical
    -- booking arithmetic.
    -- ------------------------------------------------------------------
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b,
        (clock_timestamp() at time zone 'utc')::date,
        v_peer_row.reserved_usd, round(v_peer_row.reserved_usd / 4, 10),
        'warmed', 300, 60, null);
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    select * into v_ledger from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_peer_row.reserved_usd <> 0
       or v_peer_row.spent_usd <> v_ledger.spent_usd
       or v_peer_row.spent_usd <= 0 then
        raise exception 'warmed must settle both books identically: % / %',
            to_jsonb(v_peer_row), to_jsonb(v_ledger);
    end if;

    -- spent_unknown books the RESERVATION on both books: the ping may have been
    -- charged and nothing priced it.
    update public.warm_customer_budget budget
       set reserved_usd = round(v_reserve, 10), spent_usd = 0
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b,
        (clock_timestamp() at time zone 'utc')::date,
        round(v_reserve, 10), 0, 'spent_unknown', 300, 60, null);
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_peer_row.reserved_usd <> 0
       or v_peer_row.spent_usd <> round(v_reserve, 10) then
        raise exception 'spent_unknown must book the reservation on the envelope: %',
            to_jsonb(v_peer_row);
    end if;

    -- release returns the reservation and books nothing.
    update public.warm_customer_budget budget
       set reserved_usd = round(v_reserve, 10), spent_usd = 0
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b,
        (clock_timestamp() at time zone 'utc')::date,
        round(v_reserve, 10), 0, 'release', 300, 60, null);
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_peer_row.reserved_usd <> 0 or v_peer_row.spent_usd <> 0 then
        raise exception 'release must return the envelope reservation and book nothing: %',
            to_jsonb(v_peer_row);
    end if;

    -- The period comes from p_budget_day, never the wall clock: a ping claimed
    -- last month settles against last month's row.
    -- reserved_day is the day the reservation was WRITTEN on, which for a ping
    -- claimed on the 31st is the 31st. The same-day release guard keys on that
    -- day, not on the wall clock, which is exactly what lets a cross-month
    -- settle still release.
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd,
        reserved_usd, reserved_day, source)
    values (v_org, 'anthropic', (v_period - interval '1 month')::date,
            v_peer::text, round(v_reserve * 10, 10), round(v_reserve, 10),
            (v_period - interval '1 day')::date, 'auto');
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b, (v_period - interval '1 day')::date,
        round(v_reserve, 10), round(v_reserve / 2, 10), 'warmed', 300, 60, null);
    select * into v_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text
       and budget.period_start = (v_period - interval '1 month')::date;
    if v_row.reserved_usd <> 0 or v_row.spent_usd <> round(v_reserve / 2, 10) then
        raise exception 'the settle period must derive from p_budget_day: %',
            to_jsonb(v_row);
    end if;
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text
       and budget.period_start = v_period;
    if v_peer_row.reserved_usd <> 0 or v_peer_row.spent_usd <> 0 then
        raise exception 'a prior-month settle must not touch the current month: %',
            to_jsonb(v_peer_row);
    end if;
    delete from public.warm_customer_budget
     where organization_id = v_org
       and period_start = (v_period - interval '1 month')::date;

    -- ------------------------------------------------------------------
    -- THE STALE-RESERVATION SELF-HEAL.
    --
    -- reserved_usd is released by warm_ping_settle, and some reservations never
    -- reach one: a worker killed between claim and settle, or a customer erased
    -- so settle can no longer name the row. public.warm_budget_ledger survives
    -- that because it is DAY-keyed -- tomorrow is a different row. This table is
    -- MONTH-keyed, so without a day on the row the stranded dollars would hold
    -- the customer's envelope hostage until the 1st.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    delete from public.warm_customer_budget budget
     where budget.organization_id = v_org;
    -- The settle assertions above booked pings against these rows, so the
    -- churn counter and the per-customer day counter are reset with the
    -- schedule: this case is about the envelope, not about either of those.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0, pings_today_date = null,
           consecutive_misses = 0, state = 'active',
           expires_at = clock_timestamp() + interval '1 day'
     where prefix.organization_id = v_org;

    -- An envelope with room for exactly one ping, entirely consumed by a
    -- reservation nobody ever released -- and stamped YESTERDAY.
    select prefix.ping_reserve_usd into v_reserve
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd,
        reserved_usd, reserved_day, source)
    values (v_org, 'anthropic', v_period, v_peer::text,
            round(v_reserve * 1.5, 10), round(v_reserve * 1.5, 10),
            (clock_timestamp() at time zone 'utc')::date - 1, 'auto');

    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0) as t(value);

    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    -- THE POINT: yesterday's stranded reservation is zeroed before the check
    -- reads it, so today's claim is admitted rather than denied forever.
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_peer
           and entry.decision = 'pinged'
    ) then
        raise exception 'a stranded yesterday-reservation must not deny today''s claim: %',
            to_jsonb(v_peer_row);
    end if;
    if v_peer_row.reserved_day <> (clock_timestamp() at time zone 'utc')::date
       or v_peer_row.reserved_usd <> round(v_reserve, 10) then
        raise exception 'the self-heal must reset the reservation to today''s only: %',
            to_jsonb(v_peer_row);
    end if;

    -- And a LATE settle for the stranded day cannot release today's
    -- reservation: the guard keys on the day the reservation was written.
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b,
        (clock_timestamp() at time zone 'utc')::date - 1,
        round(v_reserve * 1.5, 10), 0, 'release', 300, 60, null);
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_peer_row.reserved_usd <> round(v_reserve, 10) then
        raise exception 'a stale release must not consume today''s reservation: %',
            to_jsonb(v_peer_row);
    end if;
    -- The control: a SAME-day release does return it, so the guard above is
    -- not simply refusing every release.
    perform public.warm_ping_settle(
        v_org, v_peer, 'anthropic', v_hash_b,
        (clock_timestamp() at time zone 'utc')::date,
        round(v_reserve, 10), 0, 'release', 300, 60, null);
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_peer_row.reserved_usd <> 0 then
        raise exception 'a same-day release must return the reservation: %',
            to_jsonb(v_peer_row);
    end if;
    -- Restore the two rows section 4 left behind, so everything below runs
    -- against the state it was written for.
    delete from public.warm_customer_budget budget
     where budget.organization_id = v_org;
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd,
        source)
    values (v_org, 'anthropic', v_period, v_cust::text,
            round(v_reserve / 2, 10), 'org_override'),
           (v_org, 'anthropic', v_period, v_peer::text,
            round(v_reserve * 10, 10), 'auto');

    -- ------------------------------------------------------------------
    -- 5. warm_customer_budget_put: sets a ceiling, never restates an account,
    -- and refuses a customer that is not this organization's.
    -- ------------------------------------------------------------------
    update public.warm_customer_budget budget
       set reserved_usd = 1.5, spent_usd = 2.5
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    perform public.warm_customer_budget_put(
        v_org, 'anthropic', v_cust, null, 0.25);
    select * into v_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    if v_row.envelope_usd <> 0.25 or v_row.reserved_usd <> 1.5
       or v_row.spent_usd <> 2.5 or v_row.source <> 'org_override' then
        raise exception 'put must set the ceiling and preserve booked money: %',
            to_jsonb(v_row);
    end if;
    v_raised := false;
    begin
        perform public.warm_customer_budget_put(
            v_org, 'anthropic', '00000000-0000-4000-8000-00000000dead'::uuid,
            null, 1.0);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'put must reject a customer of another organization';
    end if;
    if not exists (
        select 1 from public.warm_customer_budget_list(v_org, 'anthropic', v_period)
                    as listed(record)
         where (listed.record ->> 'customer_ref') = v_cust::text
           and (listed.record ->> 'erased')::boolean is false
    ) then
        raise exception 'warm_customer_budget_list must project the live row';
    end if;

    -- ------------------------------------------------------------------
    -- 6. The allocator: index-mass shares, raise-only, override-immune.
    -- ------------------------------------------------------------------
    delete from public.warm_customer_budget where organization_id = v_org;
    delete from public.warm_decision_log where organization_id = v_org;
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count,
        index_score)
    values (v_org, v_cust, 'anthropic', v_hash_a, 'pinged',
            0.5, 0.35, 0.1, 1000, 10, 3.0),
           (v_org, v_peer, 'anthropic', v_hash_b, 'skipped_lambda',
            0.5, 0.35, 0.1, 1000, 10, 1.0),
           -- Negative mass is clamped at zero: a candidate below break-even
           -- generated no positive demand to apportion.
           (v_org, v_peer, 'anthropic', v_hash_b, 'skipped_roi',
            0.1, 0.35, 0.1, 1000, 10, -50.0);
    perform public.warm_customer_budget_allocate(200);
    select * into v_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_row.source <> 'auto' or v_peer_row.source <> 'auto' then
        raise exception 'the allocator must write auto rows';
    end if;
    if abs(v_row.envelope_usd
           - round(10.0 * extract(day from (v_period + interval '1 month'
                                            - interval '1 day'))::integer * 0.75, 10)) > 0.000001
       or abs(v_peer_row.envelope_usd / v_row.envelope_usd - (1.0 / 3.0)) > 0.000001 then
        raise exception 'the allocator must split by positive index mass 3:1: % / %',
            to_jsonb(v_row), to_jsonb(v_peer_row);
    end if;
    -- Raise-only, override-immune.
    update public.warm_customer_budget budget
       set envelope_usd = 999.0
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    update public.warm_customer_budget budget
       set envelope_usd = 0.5, source = 'org_override'
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    perform public.warm_customer_budget_allocate(200);
    select * into v_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    select * into v_peer_row from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_peer::text;
    if v_row.envelope_usd <> 999.0 then
        raise exception 'the allocator must never lower an auto envelope: %',
            to_jsonb(v_row);
    end if;
    if v_peer_row.envelope_usd <> 0.5 or v_peer_row.source <> 'org_override' then
        raise exception 'the allocator must never touch an org_override row: %',
            to_jsonb(v_peer_row);
    end if;
    -- With no index history at all the split is equal over customers with an
    -- active prefix.
    delete from public.warm_customer_budget where organization_id = v_org;
    delete from public.warm_decision_log where organization_id = v_org;
    perform public.warm_customer_budget_allocate(200);
    select count(distinct budget.envelope_usd) into v_count
      from public.warm_customer_budget budget
     where budget.organization_id = v_org;
    if v_count <> 1 then
        raise exception 'a zero-mass allocation must be an equal split';
    end if;

    -- ------------------------------------------------------------------
    -- 7. Erasure TOMBSTONES the key and keeps the dollars. Subject deletion of
    -- a warming customer is the one place where a money row survives an
    -- erasure, so both halves are asserted.
    -- ------------------------------------------------------------------
    update public.warm_customer_budget budget
       set reserved_usd = 1.25, spent_usd = 3.5
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    select budget.envelope_usd into v_reserve
      from public.warm_customer_budget budget
     where budget.organization_id = v_org and budget.customer_ref = v_cust::text;
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_request, v_org, 'delete', 'customer', v_cust, 'processing',
        'evidence:warm-envelope:delete', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-envelope',
        'system:warm-envelope', now()),
       (v_export_request, v_org, 'export', 'customer', v_cust, 'processing',
        'evidence:warm-envelope:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-envelope',
        'system:warm-envelope', now());
    -- Live, the subject's own envelope IS portable to them.
    if not exists (
        select 1 from public.compliance_export_subject(
            v_org, v_export_request, 'brevitas_admin:assertion') as exported(record)
         where exported.record ->> 'record_type' = 'warming_customer_budget'
           and exported.record -> 'data' ->> 'customer_ref' = v_cust::text
    ) then
        raise exception 'the subject export must carry the live envelope';
    end if;
    perform public.compliance_delete_subject(
        v_org, v_request, 'brevitas_admin:assertion');
    if exists (
        select 1 from public.warm_customer_budget budget
         where budget.organization_id = v_org and budget.customer_ref = v_cust::text
    ) then
        raise exception 'subject erasure must destroy the customer key';
    end if;
    select budget.customer_ref, budget.reserved_usd, budget.spent_usd,
           budget.envelope_usd
      into v_ref, v_row.reserved_usd, v_row.spent_usd, v_row.envelope_usd
      from public.warm_customer_budget budget
     where budget.organization_id = v_org
       and budget.customer_ref like 'erased:%';
    if v_ref !~ '^erased:[0-9a-f]{64}$' then
        raise exception 'the tombstone must be 64 hex characters: %', v_ref;
    end if;
    if v_row.reserved_usd <> 1.25 or v_row.spent_usd <> 3.5
       or v_row.envelope_usd <> v_reserve then
        raise exception 'the tombstone must preserve every dollar: %', to_jsonb(v_row);
    end if;
    -- A tombstoned row is unreachable from the subject export BY CONSTRUCTION:
    -- the projection is keyed on customer_ref = subject_id::text, and a random
    -- 'erased:<hex>' token can never equal a uuid. It cannot be asserted by
    -- calling the export after the erasure -- the subject row is gone, so
    -- compliance_export_subject refuses the request outright -- so what is
    -- asserted is the predicate itself, which is the thing that could regress.
    if pg_catalog.pg_get_functiondef('public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       not like '%budget.customer_ref = v_request.subject_id::text%' then
        raise exception 'the subject envelope export must be keyed on the subject id';
    end if;
    if pg_catalog.pg_get_functiondef('public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
       not like '%warming_customer_budget%'
       or pg_catalog.pg_get_functiondef('public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       not like '%warming_customer_budget%' then
        raise exception 'the compliance exports do not carry the envelopes';
    end if;

    -- ------------------------------------------------------------------
    -- 8. Retention. The envelopes age on the EVIDENCE horizon (dollar
    -- accounting, not behavioural telemetry), fenced by preservation hold like
    -- every other tenant class.
    -- ------------------------------------------------------------------
    update public.warm_customer_budget budget
       set period_start = (clock_timestamp() - interval '500 days')::date
     where budget.organization_id = v_org;
    update public.warm_customer_budget budget
       set period_start = date_trunc('month', budget.period_start::timestamp)::date
     where budget.organization_id = v_org;
    select count(*) into v_count from public.warm_customer_budget
     where organization_id = v_org;
    if v_count = 0 then
        raise exception 'the retention fixture has no rows to sweep';
    end if;
    v_result := public.compliance_run_retention(
        v_run, 'brevitas_admin:assertion', 100, false);
    if (v_result ->> 'warm_customer_budget_candidates')::integer < v_count then
        raise exception 'the dry run must count the aged envelopes: %', v_result;
    end if;
    if (v_result ->> 'warm_customer_budget_deleted')::integer <> 0 then
        raise exception 'a dry run must delete nothing: %', v_result;
    end if;
    v_result := public.compliance_run_retention(
        v_run, 'brevitas_admin:assertion', 100, true);
    if (v_result ->> 'warm_customer_budget_deleted')::integer < v_count then
        raise exception 'the apply must delete the aged envelopes: %', v_result;
    end if;
    if exists (select 1 from public.warm_customer_budget
                where organization_id = v_org) then
        raise exception 'the aged envelopes survived retention';
    end if;

    -- ------------------------------------------------------------------
    -- 9. Tenant erasure clears the envelopes outright. A subject erasure keeps
    -- the dollars because the ORGANIZATION still needs its accounting; a
    -- tenant erasure has no organization left to account to.
    -- ------------------------------------------------------------------
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd, source)
    values (v_org, 'anthropic', v_period, v_peer::text, 1.0, 'auto');
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_tenant_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-envelope:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-envelope',
        'system:warm-envelope');
    perform public.compliance_delete_tenant(
        v_org, v_tenant_request, 'brevitas_admin:assertion');
    if exists (select 1 from public.warm_customer_budget
                where organization_id = v_org) then
        raise exception 'tenant erasure must clear the envelopes';
    end if;

    raise notice '202608100004 envelope assertions passed';
end;
$$;

rollback;
