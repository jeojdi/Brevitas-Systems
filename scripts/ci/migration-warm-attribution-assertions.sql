-- Phase 1.5 prefix graph (202608100008): airport-game attribution.
--
-- The claim: warming spend can be divided honestly. What this file pins is the
-- narrow set of properties that makes that claim checkable -- the split sums to
-- the ping EXACTLY, a window nobody arrived in allocates NOTHING to anybody,
-- overlapping pings are Brevitas's waste rather than a customer's bill, a
-- closed day is never restated, and none of it is ever a billed quantity.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000c0001';
    v_org uuid := '00000000-0000-4000-8000-0000000c0002';
    v_alpha uuid := '00000000-0000-4000-8000-0000000c0003';
    v_beta uuid := '00000000-0000-4000-8000-0000000c0004';
    v_gamma uuid := '00000000-0000-4000-8000-0000000c0005';
    v_hash_alpha text := repeat('c1', 32);
    v_hash_beta text := repeat('c2', 32);
    v_hash_gamma text := repeat('c3', 32);
    v_run_signature text := 'public.warm_attribution_run(integer,integer)';
    v_list_signature text := 'public.warm_attribution_list(uuid,text,date,date)';
    v_residual_signature text :=
        'public.warm_attribution_residual_get(uuid,text,date,date)';
    v_root text := md5('attr-root-a') || md5('attr-root-b');
    v_shared text := md5('attr-shared-a') || md5('attr-shared-b');
    v_leaf_alpha text := md5('attr-leaf-alpha-a') || md5('attr-leaf-alpha-b');
    v_leaf_beta text := md5('attr-leaf-beta-a') || md5('attr-leaf-beta-b');
    v_lone_root text := md5('attr-lone-root-a') || md5('attr-lone-root-b');
    v_lone_leaf text := md5('attr-lone-leaf-a') || md5('attr-lone-leaf-b');
    v_ancestor text;
    v_request uuid := '00000000-0000-4000-8000-0000000c0011';
    v_export_request uuid := '00000000-0000-4000-8000-0000000c0012';
    v_subject_request uuid := '00000000-0000-4000-8000-0000000c0013';
    v_today date := (now() at time zone 'utc')::date;
    v_day date;
    v_older date;
    v_role text;
    v_table text;
    v_count integer;
    v_result jsonb;
    v_number numeric;
    v_other numeric;
    v_daily public.warm_attribution_daily%rowtype;
    v_residual public.warm_attribution_residual%rowtype;
    v_chain_alpha jsonb;
    v_chain_beta jsonb;
    v_chain_lone jsonb;
begin
    v_day := v_today - 1;
    v_older := v_today - 2;

    -- ------------------------------------------------------------------
    -- 0. Posture. Three SECURITY DEFINER routines, service_role only, and no
    -- table privilege for anybody: every read and write is through a routine.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_run_signature) is null
       or to_regprocedure(v_list_signature) is null
       or to_regprocedure(v_residual_signature) is null then
        raise exception 'the attribution routines are missing';
    end if;
    foreach v_role in array array['anon', 'authenticated'] loop
        if has_function_privilege(v_role, v_run_signature, 'execute')
           or has_function_privilege(v_role, v_list_signature, 'execute')
           or has_function_privilege(v_role, v_residual_signature, 'execute') then
            raise exception 'the attribution routines must grant % nothing', v_role;
        end if;
    end loop;
    foreach v_role in array array['service_role'] loop
        if not has_function_privilege(v_role, v_run_signature, 'execute')
           or not has_function_privilege(v_role, v_list_signature, 'execute')
           or not has_function_privilege(v_role, v_residual_signature, 'execute') then
            raise exception 'service_role must keep execute on the attribution routines';
        end if;
    end loop;
    foreach v_table in array array['warm_attribution_daily',
                                   'warm_attribution_residual',
                                   'warm_prefix_cost_miss'] loop
        if not (select relrowsecurity from pg_catalog.pg_class
                 where oid = ('public.' || v_table)::regclass) then
            raise exception 'public.% must have row level security enabled', v_table;
        end if;
        if exists (select 1 from pg_catalog.pg_policy
                    where polrelid = ('public.' || v_table)::regclass) then
            raise exception 'public.% must carry zero policies', v_table;
        end if;
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if has_table_privilege(v_role, 'public.' || v_table, 'select')
               or has_table_privilege(v_role, 'public.' || v_table, 'insert')
               or has_table_privilege(v_role, 'public.' || v_table, 'update')
               or has_table_privilege(v_role, 'public.' || v_table, 'delete') then
                raise exception 'public.% must grant % no table privilege',
                    v_table, v_role;
            end if;
        end loop;
    end loop;
    -- MEASURED-ONLY, asserted on the text rather than trusted. The job may not
    -- write a receipt, a ledger row, a settlement or a fee.
    if pg_catalog.pg_get_functiondef(v_run_signature::regprocedure) ~*
       '(insert|update|delete)[[:space:]]+(into[[:space:]]+|from[[:space:]]+)?public\.(usage_log|warm_budget_ledger|billing_ledger|billing_period_settlement|billing_period_settlement_evidence)' then
        raise exception 'the attribution job must never write the money path';
    end if;

    -- ------------------------------------------------------------------
    -- 1. Fixture. Two customers of one organization whose prefixes share a
    -- 4096-token block and then fork -- the shape the whole job exists for.
    -- ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_actor, 'warm-attribution-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm attribution fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_alpha, v_org, 'warm-attribution-alpha'),
           (v_beta, v_org, 'warm-attribution-beta'),
           (v_gamma, v_org, 'warm-attribution-gamma');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    v_ancestor := 's1.k1.r' || substr(v_root, 1, 12) || '.' || substr(v_shared, 1, 12);
    v_chain_alpha := jsonb_build_array(
        jsonb_build_object('digest', v_root, 'label', substr(v_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_shared, 'label', substr(v_shared, 1, 12),
                           'parent', v_root, 'depth', 1, 'block_tokens', 4096,
                           'token_cum', 4096, 'block_elements', 3),
        jsonb_build_object('digest', v_leaf_alpha,
                           'label', substr(v_leaf_alpha, 1, 12),
                           'parent', v_shared, 'depth', 2, 'block_tokens', 1904,
                           'token_cum', 6000, 'block_elements', 2));
    v_chain_beta := jsonb_build_array(
        jsonb_build_object('digest', v_root, 'label', substr(v_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_shared, 'label', substr(v_shared, 1, 12),
                           'parent', v_root, 'depth', 1, 'block_tokens', 4096,
                           'token_cum', 4096, 'block_elements', 3),
        jsonb_build_object('digest', v_leaf_beta,
                           'label', substr(v_leaf_beta, 1, 12),
                           'parent', v_shared, 'depth', 2, 'block_tokens', 3904,
                           'token_cum', 8000, 'block_elements', 2));
    v_chain_lone := jsonb_build_array(
        jsonb_build_object('digest', v_lone_root,
                           'label', substr(v_lone_root, 1, 12),
                           'parent', '', 'depth', 0, 'block_tokens', 0,
                           'token_cum', 0, 'block_elements', 0),
        jsonb_build_object('digest', v_lone_leaf,
                           'label', substr(v_lone_leaf, 1, 12),
                           'parent', v_lone_root, 'depth', 1,
                           'block_tokens', 4096, 'token_cum', 4096,
                           'block_elements', 3));
    -- DELIBERATELY NOT token_cum. warm_prefixes.prefix_tokens is counted on a
    -- different basis than warm_prefix_node.token_cum -- text-only versus
    -- canonical JSON on the anthropic path -- and measured real prefixes run
    -- 4-12% BELOW token_cum. Seeding the two equal is what let a split
    -- normalized on prefix_tokens hand the shared ancestors more than the whole
    -- ping and leave the leaf a negative share, unseen. These two arms are
    -- seeded 10% low so section 5's numbers only hold if the split normalizes
    -- on the chain's own basis.
    perform public.warm_prefix_observe(
        v_org, v_alpha, 'anthropic', v_hash_alpha, 'enc:payload', 5400,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_alpha,
        v_ancestor || '.' || substr(v_leaf_alpha, 1, 12), 1);
    perform public.warm_prefix_observe(
        v_org, v_beta, 'anthropic', v_hash_beta, 'enc:payload', 7200,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_beta,
        v_ancestor || '.' || substr(v_leaf_beta, 1, 12), 1);
    perform public.warm_prefix_observe(
        v_org, v_gamma, 'anthropic', v_hash_gamma, 'enc:payload', 5000,
        300, 60, false, 0.375, 'claude-sonnet-4-5', v_chain_lone,
        's1.k1.r' || substr(v_lone_root, 1, 12) || '.'
        || substr(v_lone_leaf, 1, 12), 1);

    -- ------------------------------------------------------------------
    -- 2. Yesterday: one ping, two arrivals, one shared block. The ping cost a
    -- dollar; the shared block's share of it is split in half and the leaf's
    -- share belongs to the arm that owns the leaf.
    -- ------------------------------------------------------------------
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, cache_attributable,
        native_cache_discount_usd, verified_savings_usd, warm_prefix_hash, ts)
    values
        (v_org, v_alpha, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_warm',
         1.0, true, false, 0, 0, v_hash_alpha,
         (v_day::timestamp at time zone 'utc') + interval '12 hours'),
        (v_org, v_alpha, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_hit',
         0.5, true, true, 0.2, 0.15, v_hash_alpha,
         (v_day::timestamp at time zone 'utc') + interval '12 hours 30 seconds'),
        (v_org, v_beta, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_hit',
         0.5, true, true, 0.4, 0.35, v_hash_beta,
         (v_day::timestamp at time zone 'utc') + interval '12 hours 1 minute'),
        -- ORGANIC REFRESH. A read outside every warm window keeps the entry
        -- alive on the traffic's own money and allocates nothing.
        (v_org, v_gamma, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_hit',
         0.5, true, true, 0.9, 0.8, v_hash_gamma,
         (v_day::timestamp at time zone 'utc') + interval '15 hours'),
        -- NOT AUTHORITATIVE. Not evidence of anything, and not a beneficiary.
        (v_org, v_beta, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_hit',
         0.5, false, true, 9.9, 9.9, v_hash_beta,
         (v_day::timestamp at time zone 'utc') + interval '12 hours 2 minutes');

    -- ------------------------------------------------------------------
    -- 3. The day before: two overlapping pings and nobody home. Every dollar
    -- of it is Brevitas's -- part waste, part failed bet -- and not one cent
    -- of it reaches a customer row.
    -- ------------------------------------------------------------------
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count, ts)
    values (v_org, v_alpha, 'anthropic', v_hash_alpha, 'pinged', 0.42, 0.11,
            1.0, 6000, 9,
            (v_older::timestamp at time zone 'utc') + interval '11 hours');
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, warm_prefix_hash, ts)
    values
        (v_org, v_alpha, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_warm',
         1.0, true, v_hash_alpha,
         (v_older::timestamp at time zone 'utc') + interval '12 hours'),
        (v_org, v_alpha, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_warm',
         0.6, true, v_hash_alpha,
         (v_older::timestamp at time zone 'utc') + interval '12 hours 2 minutes');
    -- An UNPRICED ping is excluded from the split and reported beside it: a
    -- cost with no number cannot be divided, and guessing one would be the
    -- invention this job exists not to make.
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, warm_prefix_hash, ts)
    values (v_org, v_beta, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_warm',
            null, true, v_hash_beta,
            (v_older::timestamp at time zone 'utc') + interval '13 hours');

    v_result := public.warm_attribution_run(2, 7);
    if coalesce((v_result ->> 'days_scanned')::integer, 0) <> 2 then
        raise exception 'the job must scan both open days: %', v_result;
    end if;

    -- ------------------------------------------------------------------
    -- 4. The split telescopes to the ping EXACTLY, and conservation holds.
    -- ------------------------------------------------------------------
    select * into v_residual from public.warm_attribution_residual residual
     where residual.organization_id = v_org and residual.provider = 'anthropic'
       and residual.day = v_day;
    if v_residual.total_warm_spend_usd <> 1.0 then
        raise exception 'yesterday priced % of warming spend',
            v_residual.total_warm_spend_usd;
    end if;
    if abs(v_residual.allocated_usd + v_residual.unallocated_speculative_usd
           + v_residual.redundancy_usd - v_residual.total_warm_spend_usd)
       > 0.000000001 then
        raise exception 'conservation failed on the open day: %',
            to_jsonb(v_residual);
    end if;
    if v_residual.allocated_usd <> 1.0
       or v_residual.unallocated_speculative_usd <> 0
       or v_residual.redundancy_usd <> 0 then
        raise exception 'a fully-served ping must allocate in full: %',
            to_jsonb(v_residual);
    end if;

    -- ------------------------------------------------------------------
    -- 5. The airport rule. The shared block is split in half between the two
    -- customers who arrived in its window; the leaf block belongs to the one
    -- arm that reads it. Gamma arrived outside every window and pays nothing.
    -- ------------------------------------------------------------------
    select count(*) into v_count from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day;
    if v_count <> 3 then
        raise exception 'yesterday must produce one row per reader, got %', v_count;
    end if;
    select * into v_daily from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_beta::text;
    -- 1.00 * 4096/6000 = 0.6826666667, halved. The denominator is the LEAF's
    -- token_cum (6000), NOT the arm's prefix_tokens (5400) -- normalizing on
    -- the smaller, differently-counted number would give 0.3792592593 here and
    -- push the leaf's remainder toward, and eventually past, zero.
    if abs(v_daily.warming_cost_share_usd - 0.3413333333) > 0.0000000002 then
        raise exception 'the shared block was not split in half: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.nodes_shared <> 1 or v_daily.nodes_read <> 3 then
        raise exception 'the sibling read three nodes and shared one: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.avg_split_denominator <> 2 then
        raise exception 'the sibling only ever split two ways: %',
            to_jsonb(v_daily);
    end if;
    -- BENEFIT IS ON THE LEAF WINDOW AND IS NEVER FANNED. Beta shared the
    -- warmed block and pays for it, but the ping never warmed beta's OWN leaf,
    -- so beta is credited nothing. Cost flows down the tree; benefit does not
    -- flow up it. This asymmetry is deliberate and conservative: crediting the
    -- sibling would claim a saving no window on its leaf can prove.
    if v_daily.warm_attributed_savings_usd <> 0 then
        raise exception 'benefit must never be fanned to an unwarmed leaf: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.verified_savings_usd <> 0.35 then
        raise exception 'the verified savings display copy is wrong: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.net_usd <> v_daily.warm_attributed_savings_usd
                          - v_daily.warming_cost_share_usd then
        raise exception 'net is savings minus cost share: %', to_jsonb(v_daily);
    end if;

    select * into v_daily from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_alpha::text;
    if abs(v_daily.warming_cost_share_usd - 0.6586666667) > 0.0000000002 then
        raise exception 'the leaf share belongs to the arm that reads it: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.avg_split_denominator <> 1.5 then
        raise exception 'alpha split one node two ways and one alone: %',
            to_jsonb(v_daily);
    end if;
    -- Alpha's own leaf WAS warmed, and alpha arrived inside that window, so
    -- alpha is credited its own discount -- once, not once per ancestor.
    if v_daily.warm_attributed_savings_usd <> 0.2 then
        raise exception 'benefit must be credited once per arrival: %',
            to_jsonb(v_daily);
    end if;

    -- ORGANIC. Gamma's only arrival was outside every warm window, so it holds
    -- a row (it read something) with nothing allocated to it and nothing
    -- credited to it.
    select * into v_daily from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_gamma::text;
    if v_daily.warming_cost_share_usd <> 0
       or v_daily.warm_attributed_savings_usd <> 0
       or v_daily.avg_split_denominator is not null then
        raise exception 'an organic refresh must allocate nothing: %',
            to_jsonb(v_daily);
    end if;

    -- ------------------------------------------------------------------
    -- 6. The dummy axiom. Two overlapping pings, no arrivals: the cheaper of
    -- the two prices the window, the excess is redundancy, the priced window
    -- itself is speculative, and BOTH land on Brevitas.
    -- ------------------------------------------------------------------
    select * into v_residual from public.warm_attribution_residual residual
     where residual.organization_id = v_org and residual.provider = 'anthropic'
       and residual.day = v_older;
    if v_residual.total_warm_spend_usd <> 1.6 then
        raise exception 'the closed-over day priced % of spend',
            v_residual.total_warm_spend_usd;
    end if;
    if v_residual.allocated_usd <> 0 then
        raise exception 'a ping nobody read must allocate nothing: %',
            to_jsonb(v_residual);
    end if;
    if abs(v_residual.redundancy_usd - 1.0) > 0.000000001
       or abs(v_residual.unallocated_speculative_usd - 0.6) > 0.000000001 then
        raise exception 'the redundancy collapse is wrong: %',
            to_jsonb(v_residual);
    end if;
    if abs(v_residual.allocated_usd + v_residual.unallocated_speculative_usd
           + v_residual.redundancy_usd - v_residual.total_warm_spend_usd)
       > 0.000000001 then
        raise exception 'conservation failed on the speculative day: %',
            to_jsonb(v_residual);
    end if;
    -- The unpriced ping is reported, never split, and never counted as spend.
    if v_residual.unpriced_usd <= 0 then
        raise exception 'the unpriced ping must be reported: %',
            to_jsonb(v_residual);
    end if;
    if exists (select 1 from public.warm_attribution_daily daily
                where daily.organization_id = v_org and daily.day = v_older) then
        raise exception 'a day with no arrivals must produce no customer rows';
    end if;
    -- The failed bet is itemized beside the beneficiary the policy predicted.
    select count(*) into v_count from public.warm_prefix_cost_miss miss
     where miss.organization_id = v_org and miss.day = v_older;
    if v_count <> 2 then
        raise exception 'both un-served windows must be itemized, got %', v_count;
    end if;
    if not exists (
        select 1 from public.warm_prefix_cost_miss miss
         where miss.organization_id = v_org and miss.day = v_older
           and miss.node_digest = v_shared
           and miss.predicted_customer_ref = v_alpha::text
           and miss.p_return = 0.42
    ) then
        raise exception 'the miss log must carry the predicted beneficiary';
    end if;
    if (select sum(miss.usd) from public.warm_prefix_cost_miss miss
         where miss.organization_id = v_org and miss.day = v_older)
       <> v_residual.unallocated_speculative_usd then
        raise exception 'the miss log and the speculative residual disagree';
    end if;

    -- ------------------------------------------------------------------
    -- 7. Idempotence and closed days. Re-running restates the open days and
    -- nothing else; a day past the close horizon is never touched again.
    -- ------------------------------------------------------------------
    delete from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_beta::text;
    v_result := public.warm_attribution_run(2, 7);
    select count(*) into v_count from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day;
    if v_count <> 3 then
        raise exception 'an open day must be recomputed, got % rows', v_count;
    end if;
    delete from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_beta::text;
    -- p_close_days = 1 closes yesterday. The statement stays as it is, hole
    -- and all: history is not the job's to rewrite.
    v_result := public.warm_attribution_run(2, 1);
    if coalesce((v_result ->> 'days_scanned')::integer, 9) <> 0 then
        raise exception 'a closed day must not be scanned: %', v_result;
    end if;
    select count(*) into v_count from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day;
    if v_count <> 2 then
        raise exception 'a closed day must not be recomputed, got % rows', v_count;
    end if;
    v_result := public.warm_attribution_run(2, 7);

    -- ------------------------------------------------------------------
    -- 8. The depth bound. An ancestry that cannot be walked to a root is
    -- attributed FLAT -- the whole ping on the leaf -- rather than spread over
    -- a span nobody can prove was shared.
    -- ------------------------------------------------------------------
    delete from public.warm_prefix_node node
     where node.organization_id = v_org and node.node_digest = v_lone_root;
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, warm_prefix_hash, ts)
    values (v_org, v_gamma, 'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_warm',
            2.0, true, v_hash_gamma,
            (v_day::timestamp at time zone 'utc') + interval '14 hours 59 minutes');
    v_result := public.warm_attribution_run(2, 7);
    select * into v_daily from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_gamma::text;
    if v_daily.warming_cost_share_usd <> 2.0 then
        raise exception 'a flat leaf must carry the whole ping: %',
            to_jsonb(v_daily);
    end if;
    if v_daily.nodes_read <> 1 then
        raise exception 'a flat leaf fans to itself only: %', to_jsonb(v_daily);
    end if;
    select * into v_residual from public.warm_attribution_residual residual
     where residual.organization_id = v_org and residual.provider = 'anthropic'
       and residual.day = v_day;
    if abs(v_residual.allocated_usd + v_residual.unallocated_speculative_usd
           + v_residual.redundancy_usd - v_residual.total_warm_spend_usd)
       > 0.000000001 then
        raise exception 'conservation failed with a flat leaf: %',
            to_jsonb(v_residual);
    end if;

    -- ------------------------------------------------------------------
    -- 9. The read paths. Org-scoped, and they never emit a digest or a path.
    -- ------------------------------------------------------------------
    select count(*) into v_count
      from public.warm_attribution_list(v_org, 'anthropic', v_older, v_today) row;
    if v_count < 3 then
        raise exception 'the list read must return the statement, got %', v_count;
    end if;
    if exists (
        select 1 from public.warm_attribution_list(
            v_org, null, v_older, v_today) row
         where row::text like '%' || v_shared || '%'
            or row::text like '%s1.k1.r%'
    ) then
        raise exception 'the attribution read must never disclose the tree';
    end if;
    if not exists (
        select 1 from public.warm_attribution_residual_get(
            v_org, 'anthropic', v_older, v_today) row
         where (row ->> 'day')::date = v_older
    ) then
        raise exception 'the residual read must return the footer';
    end if;

    -- ------------------------------------------------------------------
    -- 10. Compliance. Tenant export carries the statement and its footer and
    -- never a digest; subject erasure tombstones the key and keeps every
    -- dollar; tenant erasure takes all three tables outright.
    -- ------------------------------------------------------------------
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_export_request, v_org, 'export', 'tenant', 'approved',
        'evidence:warm-attribution:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-attribution',
        'system:warm-attribution');
    select count(*) into v_count
      from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
     where record ->> 'record_type' in ('warming_attribution_daily',
                                        'warming_attribution_residual');
    if v_count < 2 then
        raise exception 'the tenant export must carry the attribution statement';
    end if;
    if exists (
        select 1 from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
         where record::text like '%' || v_shared || '%'
    ) then
        raise exception 'the tenant export must never disclose node digests';
    end if;
    if pg_catalog.pg_get_functiondef(
           'public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       not like '%statement.customer_ref = v_request.subject_id::text%' then
        raise exception 'the subject attribution export must be keyed on the subject';
    end if;

    select daily.warming_cost_share_usd into v_number
      from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_beta::text;
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_subject_request, v_org, 'delete', 'customer', v_beta, 'processing',
        'evidence:warm-attribution:subject', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-attribution',
        'system:warm-attribution', now());
    perform public.compliance_delete_subject(
        v_org, v_subject_request, 'brevitas_admin:assertion');
    if exists (select 1 from public.warm_attribution_daily daily
                where daily.organization_id = v_org
                  and daily.customer_ref = v_beta::text) then
        raise exception 'subject erasure must destroy the customer key';
    end if;
    select daily.warming_cost_share_usd into v_other
      from public.warm_attribution_daily daily
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref like 'erased:%';
    if v_other is distinct from v_number then
        raise exception 'subject erasure must preserve every dollar (% vs %)',
            v_other, v_number;
    end if;
    if exists (select 1 from public.warm_prefix_cost_miss miss
                where miss.organization_id = v_org
                  and miss.predicted_customer_ref = v_beta::text) then
        raise exception 'subject erasure must destroy the predicted beneficiary';
    end if;

    -- ------------------------------------------------------------------
    -- 11. Retention. Dollar accounting on the 400-day evidence horizon, the
    -- node-keyed miss log on the tree's own 90-day horizon.
    -- ------------------------------------------------------------------
    update public.warm_attribution_daily daily
       set day = v_today - 401
     where daily.organization_id = v_org and daily.day = v_day
       and daily.customer_ref = v_alpha::text;
    update public.warm_attribution_residual residual
       set day = v_today - 401
     where residual.organization_id = v_org and residual.day = v_older;
    update public.warm_prefix_cost_miss miss
       set day = v_today - 91
     where miss.organization_id = v_org;
    v_result := public.purge_warm_state(7);
    if coalesce((v_result ->> 'attribution_daily_deleted')::integer, 0) < 1
       or coalesce((v_result ->> 'attribution_residual_deleted')::integer, 0) < 1
       or coalesce((v_result ->> 'attribution_misses_deleted')::integer, 0) < 1 then
        raise exception 'retention did not prune the attribution horizons: %',
            v_result;
    end if;
    if exists (select 1 from public.warm_attribution_daily daily
                where daily.organization_id = v_org and daily.day < v_today - 400) then
        raise exception 'retention left a stale statement behind';
    end if;

    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-attribution:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-attribution',
        'system:warm-attribution');
    perform public.compliance_delete_tenant(
        v_org, v_request, 'brevitas_admin:assertion');
    foreach v_table in array array['warm_attribution_daily',
                                   'warm_attribution_residual',
                                   'warm_prefix_cost_miss'] loop
        execute format(
            'select count(*) from public.%I row where row.organization_id = $1',
            v_table) into v_count using v_org;
        if v_count <> 0 then
            raise exception 'tenant erasure left % rows in %', v_count, v_table;
        end if;
    end loop;

    raise notice 'warm attribution assertions passed';
end;
$$;

rollback;
