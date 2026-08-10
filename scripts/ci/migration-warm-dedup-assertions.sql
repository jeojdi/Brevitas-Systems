-- Phase 1.5 prefix graph (202608100007): shared-parent warming dedup.
--
-- The claim: one provider cache entry, bought once. What this file pins is the
-- narrow set of properties that makes that safe -- the flag is off by default
-- and off means BYTE-IDENTICAL, a deferral only ever follows a leader that was
-- actually claimed, a deferral moves no money, and a group only forms over a
-- node the provider would really cache and really still holds.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000d0001';
    v_org uuid := '00000000-0000-4000-8000-0000000d0002';
    v_lead uuid := '00000000-0000-4000-8000-0000000d0003';
    v_peer uuid := '00000000-0000-4000-8000-0000000d0004';
    v_hash_lead text := repeat('d1', 32);
    v_hash_peer text := repeat('d2', 32);
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean)';
    v_record_signature text := 'public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text)';
    v_root text := md5('dedup-root-a') || md5('dedup-root-b');
    v_shared text := md5('dedup-shared-a') || md5('dedup-shared-b');
    v_leaf_lead text := md5('dedup-leaf-lead-a') || md5('dedup-leaf-lead-b');
    v_leaf_peer text := md5('dedup-leaf-peer-a') || md5('dedup-leaf-peer-b');
    v_alt_root text := md5('dedup-alt-root-a') || md5('dedup-alt-root-b');
    v_alt_leaf text := md5('dedup-alt-leaf-a') || md5('dedup-alt-leaf-b');
    v_ancestor text;
    v_path_lead text;
    v_path_peer text;
    v_path_alt text;
    v_chain_lead jsonb;
    v_chain_peer jsonb;
    v_chain_alt jsonb;
    v_bucket text;
    v_column text;
    v_count integer;
    v_reserve_lead numeric;
    v_reserve_peer numeric;
    v_reserved numeric;
    v_ungrouped_hit numeric;
    v_reserved_off numeric;
    v_row public.warm_decision_log%rowtype;
    v_due timestamptz;
    v_raised boolean;
    v_claim jsonb;
begin
    -- ------------------------------------------------------------------
    -- 0. Signatures. The sixteen-argument claim and the twenty-three-argument
    -- decision recorder are GONE, not overloaded: two candidates for one name
    -- is how half the callers keep talking to the pre-dedup policy.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is not null then
        raise exception 'the seventeen-argument warm_due_claim must be the only one';
    end if;
    if to_regprocedure(v_record_signature) is null
       or to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)') is not null then
        raise exception 'the twenty-seven-argument warm_decision_record must be the only one';
    end if;
    foreach v_column in array array['anon', 'authenticated'] loop
        if has_function_privilege(v_column, v_claim_signature, 'execute')
           or has_function_privilege(v_column, v_record_signature, 'execute') then
            raise exception 'the dedup claim path must grant % nothing', v_column;
        end if;
    end loop;
    if not has_function_privilege('service_role', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_record_signature, 'execute') then
        raise exception 'service_role must keep execute on the dedup claim path';
    end if;

    -- ------------------------------------------------------------------
    -- 1. The decision-log stamp. Four nullable columns and four constraints;
    -- a group id without a role (or the reverse) is a half-written stamp and
    -- 202608100008 would read it as one or the other.
    -- ------------------------------------------------------------------
    foreach v_column in array array[
        'dedup_group', 'dedup_role', 'dedup_group_size', 'dedup_node_digest'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute attribute
             where attribute.attrelid = 'public.warm_decision_log'::regclass
               and attribute.attname = v_column
               and not attribute.attisdropped
        ) then
            raise exception 'warm_decision_log.% is missing', v_column;
        end if;
    end loop;
    foreach v_column in array array[
        'warm_decision_log_dedup_role_check',
        'warm_decision_log_dedup_group_size_check',
        'warm_decision_log_dedup_node_digest_check',
        'warm_decision_log_dedup_pairing_check',
        'warm_decision_log_decision_check'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_constraint
             where conrelid = 'public.warm_decision_log'::regclass
               and conname = v_column
               and convalidated
        ) then
            raise exception 'warm_decision_log constraint % is missing or unvalidated', v_column;
        end if;
    end loop;
    if not pg_catalog.pg_get_constraintdef((
            select oid from pg_catalog.pg_constraint
             where conrelid = 'public.warm_decision_log'::regclass
               and conname = 'warm_decision_log_decision_check'))
           like '%dedup_deferred%' then
        raise exception 'the decision vocabulary must admit dedup_deferred';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'warm-dedup-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm dedup fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_lead, v_org, 'warm-dedup-leader'),
           (v_peer, v_org, 'warm-dedup-peer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    -- Two arms that share the root and one 120000-token block, then fork.
    -- prefix_tokens matches each arm's own leaf token_cum, because a real
    -- extractor's chain covers the whole prefix bar the tail: a fixture that
    -- divorces the two silently disables every rule expressed as a ratio of
    -- them, the deferral coverage gate among them. The
    -- shared block is the group node: above anthropic's 1024-token cache floor
    -- and, because both arms were just observed, warm.
    v_ancestor := 's1.k1.r' || substr(v_root, 1, 12) || '.' || substr(v_shared, 1, 12);
    v_path_lead := v_ancestor || '.' || substr(v_leaf_lead, 1, 12);
    v_path_peer := v_ancestor || '.' || substr(v_leaf_peer, 1, 12);
    v_path_alt := 's1.k1.r' || substr(v_alt_root, 1, 12) || '.'
                  || substr(v_alt_leaf, 1, 12);
    v_chain_lead := jsonb_build_array(
        jsonb_build_object('digest', v_root, 'label', substr(v_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_shared, 'label', substr(v_shared, 1, 12),
                           'parent', v_root, 'depth', 1, 'block_tokens', 120000,
                           'token_cum', 120000, 'block_elements', 3),
        jsonb_build_object('digest', v_leaf_lead,
                           'label', substr(v_leaf_lead, 1, 12),
                           'parent', v_shared, 'depth', 2, 'block_tokens', 30000,
                           'token_cum', 150000, 'block_elements', 2));
    v_chain_peer := jsonb_build_array(
        jsonb_build_object('digest', v_root, 'label', substr(v_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_shared, 'label', substr(v_shared, 1, 12),
                           'parent', v_root, 'depth', 1, 'block_tokens', 120000,
                           'token_cum', 120000, 'block_elements', 3),
        jsonb_build_object('digest', v_leaf_peer,
                           'label', substr(v_leaf_peer, 1, 12),
                           'parent', v_shared, 'depth', 2, 'block_tokens', 80000,
                           'token_cum', 200000, 'block_elements', 2));

    perform public.warm_prefix_observe(
        v_org, v_lead, 'anthropic', v_hash_lead, 'enc:payload', 150000,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_lead, v_path_lead, 1);
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_peer, 'enc:payload', 200000,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_peer, v_path_peer, 1);
    if (select count(*) from public.warm_prefixes prefix
         where prefix.organization_id = v_org
           and prefix.chain_path is not null) <> 2 then
        raise exception 'the fixture arms were not stamped with a chain path';
    end if;
    -- 1024 is the provider's minimum on the PROVIDER's basis; token_cum is
    -- counted on a different, higher one, so the gate carries 25% headroom and
    -- the fixture has to clear the headroomed number, not the raw one.
    if (select token_cum from public.warm_prefix_node node
         where node.organization_id = v_org and node.node_digest = v_shared)
       < ceil(1.25 * 1024) then
        raise exception 'the fixture group node is below the provider cache floor';
    end if;

    v_bucket := ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text;
    -- The leader is due FIRST. The loop order is the index order, not the
    -- group order, and 202608100007 defers only behind a leader already
    -- claimed in the same invocation -- so a fixture that wants a deferral has
    -- to put the leader in front, exactly as the implementation says.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20)
     where prefix.organization_id = v_org;
    -- The ledger reservation the claim will make, spelled the way the claim
    -- spells it: the larger of the observer-priced worst case and the flat
    -- per-Mtok floor.
    select greatest(prefix.ping_reserve_usd,
                    round(3.75 * prefix.prefix_tokens / 1000000.0, 10))
      into v_reserve_lead
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_lead;
    select greatest(prefix.ping_reserve_usd,
                    round(3.75 * prefix.prefix_tokens / 1000000.0, 10))
      into v_reserve_peer
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    if coalesce(v_reserve_lead, 0) <= 0 or v_reserve_peer <= v_reserve_lead then
        raise exception 'the fixture must price the leader strictly cheaper';
    end if;

    -- ------------------------------------------------------------------
    -- 2. OFF IS OFF. The default claim groups nothing, defers nothing and
    -- reserves twice -- 202608100005's behaviour, unchanged.
    -- ------------------------------------------------------------------
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0) as t(value);
    if v_count <> 2 then
        raise exception 'the flag-off claim must claim both arms, claimed %', v_count;
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org
           and (entry.decision = 'dedup_deferred'
                or entry.dedup_group is not null
                or entry.dedup_role is not null
                or entry.dedup_node_digest is not null)
    ) then
        raise exception 'the flag-off claim must write no dedup stamp';
    end if;
    select ledger.reserved_usd into v_reserved
      from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_reserved <> v_reserve_lead + v_reserve_peer then
        raise exception 'the flag-off claim must reserve both arms, reserved %', v_reserved;
    end if;

    -- ------------------------------------------------------------------
    -- 3. ON. One leader claimed, one member deferred, ONE reservation.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;

    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 1 then
        raise exception 'the grouped claim must claim exactly the leader, claimed %', v_count;
    end if;
    select * into v_row from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'pinged';
    if v_row.customer_id <> v_lead then
        raise exception 'the CHEAPEST member must lead, led by %', v_row.customer_id;
    end if;
    if v_row.dedup_role <> 'leader' or v_row.dedup_group is null
       or v_row.dedup_group_size <> 2
       or v_row.dedup_node_digest <> v_shared then
        raise exception 'the leader row carries the wrong group stamp: %', to_jsonb(v_row);
    end if;
    -- p_group = 1 - (1 - p_own) * (1 - h_peer). The fixture peer returns with
    -- probability 1 in this bucket, so the group's return probability is 1 and
    -- -- because p_own is already 1 -- the substitution is visible in the
    -- stamp rather than in the number. What is asserted here is that the
    -- combination never leaves the unit interval and never lowers p_own.
    if v_row.p_return < 1 or v_row.p_return > 1 then
        raise exception 'the group return probability must be 1 here, got %', v_row.p_return;
    end if;
    -- The value of a hit is the SHARED span, not the leader's whole prefix.
    if v_row.v_hit_usd is not null then
        raise exception 'the index was off, so no dollars may be logged';
    end if;

    select * into v_row from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'dedup_deferred';
    if not found or v_row.customer_id <> v_peer then
        raise exception 'the expensive member must be deferred';
    end if;
    if v_row.dedup_role <> 'deferred' or v_row.dedup_group_size <> 2
       or v_row.dedup_node_digest <> v_shared
       or v_row.claim_token is not null then
        raise exception 'the deferred row carries the wrong stamp: %', to_jsonb(v_row);
    end if;
    if (select dedup_group from public.warm_decision_log
         where organization_id = v_org and decision = 'pinged')
       <> v_row.dedup_group then
        raise exception 'the leader and its member must share one group id';
    end if;

    -- ONE charge. The deferred member moved no ledger dollar.
    select ledger.reserved_usd into v_reserved
      from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_reserved <> v_reserve_lead then
        raise exception 'the group must reserve exactly the leader, reserved %', v_reserved;
    end if;
    if exists (
        select 1 from public.warm_prefixes prefix
         where prefix.organization_id = v_org and prefix.customer_id = v_peer
           and prefix.claim_token is not null
    ) then
        raise exception 'a deferred member must take no claim token';
    end if;
    -- next_due_at advanced by the horizon the leader's ping bought: ttl less
    -- the safety margin, floored at a minute.
    select prefix.next_due_at into v_due
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    if v_due < clock_timestamp() + interval '200 seconds'
       or v_due > clock_timestamp() + interval '280 seconds' then
        raise exception 'the deferred member must be pushed to the shared horizon, got %', v_due;
    end if;

    -- ------------------------------------------------------------------
    -- 3z. THE HIT VALUE IS THE SHARED SPAN. With the index on, a grouped
    -- leader's logged v_hit_usd is scaled by token_cum(V) / prefix_tokens --
    -- the group only rides the bytes it shares -- while the RESERVATION stays
    -- the leader's full-prefix worst-case write. Conservative in both
    -- directions. index_score is untouched: it is written in ratio form
    -- (p_eff/b - 1 - n_chain), in which the prefix's dollar value cancels.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        false, 0);
    select entry.v_hit_usd into v_ungrouped_hit
      from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.customer_id = v_lead
       and entry.decision = 'pinged';
    if coalesce(v_ungrouped_hit, 0) <= 0 then
        raise exception 'the ungrouped leader must log a positive hit value';
    end if;

    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        false, 0, true);
    select * into v_row from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'pinged';
    if v_row.dedup_role <> 'leader' then
        raise exception 'the index-on claim must still group';
    end if;
    -- 120000 shared tokens out of the leader's 150000.
    if abs(v_row.v_hit_usd
           - round(v_ungrouped_hit * 120000 / 150000.0, 10)) > 1e-10 then
        raise exception 'the grouped hit value must price the shared span, got % against %',
            v_row.v_hit_usd, v_ungrouped_hit;
    end if;
    if v_row.index_score is null then
        raise exception 'the index must still be computed under dedup';
    end if;
    select ledger.reserved_usd into v_reserved
      from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_reserved <> v_reserve_lead then
        raise exception 'a scaled hit value must not scale the reservation, reserved %',
            v_reserved;
    end if;

    -- ------------------------------------------------------------------
    -- 3a. THE COMBINED HAZARD IS DIAGNOSTIC, NOT A GATE. The leader returns
    -- 5 times in 20 arrivals in this bucket, its member 8 in 20:
    --
    --   p_group = 1 - (1 - 0.25) * (1 - 0.40) = 0.55
    --
    -- p_group is reported on the leader's claim payload and NOTHING else.
    -- Every gate runs on p_own, so the leader is scored at 0.25 and logged at
    -- 0.25, and the deferred row is logged with its own 0.40: a row is never
    -- scored on one number and logged with another. Section 3b is why the
    -- substitution is not allowed to gate.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(
               v_bucket, case when prefix.customer_id = v_lead then 5 else 8 end),
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    select value into v_claim from public.warm_due_claim(
        10, 3.75, 5, 0.0, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value)
     where (value ->> 'customer_id')::uuid = v_lead;
    if abs((v_claim ->> 'dedup_p_group')::numeric - 0.55) > 1e-12 then
        raise exception 'the combined group hazard must be reported as 0.55: %',
            v_claim;
    end if;
    select * into v_row from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'pinged';
    if abs(v_row.p_return - 0.25) > 1e-12 then
        raise exception 'the leader must be scored on its OWN hazard, got %',
            v_row.p_return;
    end if;
    select * into v_row from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'dedup_deferred';
    if abs(v_row.p_return - 0.40) > 1e-12 then
        raise exception 'the deferred row must log its OWN hazard, got %', v_row.p_return;
    end if;

    -- ------------------------------------------------------------------
    -- 3b. DEDUP MAY ONLY SUBTRACT. Both arms sit at h = 0.06 under a 0.11
    -- break-even floor: with the flag OFF both are skipped_roi and the group
    -- buys nothing. Gating on the combined 1 - 0.94^2 = 0.1164 would clear the
    -- floor and BUY a ping the flag-off policy refused -- the flag paying for
    -- warming rather than saving it. Asserted as an inequality on the ledger,
    -- because that is the property the whole migration promises.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           arrival_count = 50,
           hour_histogram = jsonb_build_object(v_bucket, 3),
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, false);
    select coalesce(sum(ledger.reserved_usd), 0) into v_reserved_off
      from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_reserved_off <> 0 then
        raise exception 'the flag-off arm of the repro must buy nothing, got %',
            v_reserved_off;
    end if;
    -- Positively, so the repro cannot pass vacuously on a fixture that simply
    -- had nothing due: both arms were SCORED and both were refused.
    if (select count(*) from public.warm_decision_log entry
         where entry.organization_id = v_org
           and entry.decision = 'skipped_roi') <> 2 then
        raise exception 'the repro must score both arms and refuse both: %',
            (select jsonb_agg(entry.decision) from public.warm_decision_log entry
              where entry.organization_id = v_org);
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true);
    select coalesce(sum(ledger.reserved_usd), 0) into v_reserved
      from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_reserved > v_reserved_off then
        raise exception
            'parent dedup increased warm spend (% with the flag on vs % off)',
            v_reserved, v_reserved_off;
    end if;
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org and entry.decision = 'pinged') then
        raise exception 'dedup bought a ping the flag-off policy skipped';
    end if;
    update public.warm_prefixes prefix
       set arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20)
     where prefix.organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 3b. A DENIED LEADER AUTHORIZES NOTHING. Dedup removes a ping only when
    -- the leader's ping actually happened; a leader stopped by any gate leaves
    -- its member to run every gate itself and be claimed exactly as the
    -- pre-dedup policy would have claimed it. This is the property that makes
    -- the flag safe to turn on: it can subtract a redundant ping, never a
    -- necessary one.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    delete from public.warm_customer_budget where organization_id = v_org;
    insert into public.warm_customer_budget (
        organization_id, provider, period_start, customer_ref, envelope_usd, source)
    values (v_org, 'anthropic',
            date_trunc('month', ((clock_timestamp() at time zone 'utc')::date)::timestamp)::date,
            v_lead::text, round(v_reserve_lead / 2, 10), 'org_override');
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp()
                         - case when prefix.customer_id = v_lead
                                then interval '2 seconds' else interval '1 second' end,
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 1 then
        raise exception 'a denied leader must leave its member claimable, claimed %', v_count;
    end if;
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_lead
           and entry.decision = 'envelope_denied'
    ) then
        raise exception 'the leader must be denied by its envelope here';
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.decision = 'dedup_deferred'
    ) then
        raise exception 'a denied leader must authorize no deferral';
    end if;
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_peer
           and entry.decision = 'pinged'
    ) then
        raise exception 'the member must ping for itself when its leader was denied';
    end if;
    delete from public.warm_customer_budget where organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 4. A COLD ancestor is not a group. Nothing under the node has been
    -- touched inside the TTL, so there is no shared entry to ride and both
    -- arms must ping exactly as the old policy would have.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_touch_at = clock_timestamp() - interval '2 hours'
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 2 then
        raise exception 'a cold ancestor must group nothing, claimed %', v_count;
    end if;
    if exists (select 1 from public.warm_decision_log
                where organization_id = v_org and decision = 'dedup_deferred') then
        raise exception 'a cold ancestor must defer nothing';
    end if;

    -- ------------------------------------------------------------------
    -- 5. A node BELOW the provider cache floor is not a group node: the
    -- provider caches nothing there, so one keep-alive buys no shared warmth
    -- and the "group" would be an accounting fiction.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefix_node node
       set token_cum = 1023
     where node.organization_id = v_org and node.node_digest = v_shared;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 2 then
        raise exception 'a sub-floor node must group nothing, claimed %', v_count;
    end if;
    update public.warm_prefix_node node
       set token_cum = 120000
     where node.organization_id = v_org and node.node_digest = v_shared;

    -- ------------------------------------------------------------------
    -- 5a. HEADROOM ON THE FLOOR. token_cum is count_tokens() over canonical
    -- JSON and runs above the text-only count the provider actually sees, so a
    -- node at 1100 is over the raw 1024 minimum while its true cacheable span
    -- is under it. The provider caches nothing there and the group is the
    -- accounting fiction the floor exists to prevent.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefix_node node
       set token_cum = 1100
     where node.organization_id = v_org and node.node_digest = v_shared;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 2 then
        raise exception 'a node inside the floor headroom must group nothing, claimed %',
            v_count;
    end if;
    update public.warm_prefix_node node
       set token_cum = 120000
     where node.organization_id = v_org and node.node_digest = v_shared;

    -- ------------------------------------------------------------------
    -- 5b. COVERAGE. The leader's ping warms the leader's own prefix and
    -- nothing past the fork, and the leader rule (cheapest prefix first) picks
    -- exactly the arm that covers the least. An arm sharing a sliver of a huge
    -- prefix keeps warmth over that sliver and loses the rest, so it is NOT
    -- deferred -- it runs every gate as it would with the flag off, which
    -- still cannot spend more than the flag-off policy.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set prefix_tokens = 2000000,
           next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '2 seconds',
           claim_token = null,
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org and prefix.customer_id = v_lead;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 2 then
        raise exception
            'a thinly covered member must not be deferred, claimed %', v_count;
    end if;
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org
                  and entry.decision = 'dedup_deferred') then
        raise exception 'deferral must be gated on coverage';
    end if;
    update public.warm_prefixes prefix
       set prefix_tokens = 200000
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;

    -- ------------------------------------------------------------------
    -- 6. NEVER ACROSS SEEDS. The seed binds provider, model, TTL tier, vary
    -- headers and the cache-identity extras, so a different seed is a
    -- different root label and no shared block label can exist. Re-stamping
    -- the peer under a different root must dissolve the group.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    v_chain_alt := jsonb_build_array(
        jsonb_build_object('digest', v_alt_root, 'label', substr(v_alt_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_alt_leaf, 'label', substr(v_alt_leaf, 1, 12),
                           'parent', v_alt_root, 'depth', 1, 'block_tokens', 8000,
                           'token_cum', 200000, 'block_elements', 3));
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_peer, 'enc:payload', 200000,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_alt, v_path_alt, 1);
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20),
           last_touch_at = clock_timestamp()
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0, true) as t(value);
    if v_count <> 2 then
        raise exception 'two seeds must never group, claimed %', v_count;
    end if;
    if exists (select 1 from public.warm_decision_log
                where organization_id = v_org and dedup_group is not null) then
        raise exception 'two seeds must produce no group stamp';
    end if;

    -- ------------------------------------------------------------------
    -- 7. The recorder refuses a half-written stamp, a size of one and a
    -- digest that is not a digest -- the same three things the table's own
    -- constraints refuse, and no more.
    -- ------------------------------------------------------------------
    v_raised := false;
    begin
        perform public.warm_decision_record(
            v_org, v_lead, 'anthropic', v_hash_lead, 'pinged', 0.5, 0.11, 0.01,
            100000, 60, 20, 0, null, null, null, null, null, null, null, null,
            null, null, null,
            gen_random_uuid(), null, 2, v_shared);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a group id without a role must be refused';
    end if;
    v_raised := false;
    begin
        perform public.warm_decision_record(
            v_org, v_lead, 'anthropic', v_hash_lead, 'pinged', 0.5, 0.11, 0.01,
            100000, 60, 20, 0, null, null, null, null, null, null, null, null,
            null, null, null,
            gen_random_uuid(), 'leader', 1, v_shared);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a group of one must be refused';
    end if;
    v_raised := false;
    begin
        perform public.warm_decision_record(
            v_org, v_lead, 'anthropic', v_hash_lead, 'pinged', 0.5, 0.11, 0.01,
            100000, 60, 20, 0, null, null, null, null, null, null, null, null,
            null, null, null,
            gen_random_uuid(), 'leader', 2, 'not-a-digest');
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a non-digest group node must be refused';
    end if;
    -- ... and accepts the row the claim actually writes.
    v_claim := public.warm_decision_record(
        v_org, v_peer, 'anthropic', v_hash_peer, 'dedup_deferred', 0.5, 0.11,
        0.01, 200000, 60, 20, 0, null, null, null, null, null, null, null,
        null, null, null, null,
        gen_random_uuid(), 'deferred', 2, v_shared);
    if v_claim ->> 'decision' <> 'dedup_deferred' then
        raise exception 'dedup_deferred must be a recordable decision';
    end if;

    raise notice 'warm dedup assertions passed';
end;
$$;

rollback;
