-- 202607280037: the PLATFORM-ADMIN account view must return the SAME rows
-- before and after 202607280035.
--
-- WHY THIS FILE EXISTS. 202607280035 narrowed the org-less branch of
-- public.usage_page so an org-less key can no longer read org-owned rows that
-- merely share its owner_id string. Exactly one caller reached that branch
-- WITHOUT being a key: api/store.py:4676-4681
-- (SupabaseUsageStore.get_admin_account_detail, serving
-- GET /v1/admin/accounts/{owner_id}/usage) posts
-- {"p_key_hash": "", "p_organization_id": null, "p_owner_id": owner_id}.
-- Applying 202607280035 therefore silently rewrote the platform-admin view:
-- on production two of the three affected owners have ALL of their newest 1000
-- rows org-scoped, so window_calls would have stayed at 1000 while the rows
-- behind it became a different, older population. That regression -- not the
-- security fix -- is what made 202607280035 unapplyable, and
-- 202607280037_platform_admin_account_usage.sql is what closes it.
--
-- Section 2 is the actual proof, and it is a differential one: it installs
-- 202607170006's PRE-202607280035 usage_page predicate as a pg_temp function,
-- runs it at the admin scope, and requires the new admin RPC to return that
-- EXACT row set -- while requiring the LIVE post-202607280035 usage_page to
-- return strictly fewer rows at the same scope. So the file cannot go green
-- either by the admin read having silently narrowed, or by 202607280035 not
-- being applied at all.
--
-- Opens its own transaction and ends in ROLLBACK, so it is rerunnable,
-- order-independent, and leaves no organization, key, usage row or temp
-- function behind.
\set ON_ERROR_STOP on

begin;

-- Section 0 -- registration guards. If either migration is missing from
-- migration-fresh-manifest.txt / migration-upgrade-manifest.txt this file must
-- say so in those words rather than failing later as if something regressed.
do $$
begin
    if to_regprocedure('public.admin_account_usage_page(text,timestamptz,bigint,integer)') is null then
        raise exception
            '202607280037_platform_admin_account_usage is not applied: register it in both migration manifests';
    end if;
    if (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = 'usage_page'
           and p.prosrc like '%(usage.organization_id is null and usage.owner_id = p_owner_id)%'
       ) <> 1 then
        raise exception
            '202607280035_usage_read_tenant_scope is not applied: this file measures the admin view ACROSS that change and is meaningless without it';
    end if;
end;
$$;

-- Section 1 -- fixture. The production shape (MEASURED on wyfz 2026-07-31):
-- one owner string holds both an org-attached key and an org-less key, and its
-- usage rows are a MIX of org-scoped and org-less -- 1,543 of 7,155 for one
-- owner, 5,892 of 7,009 for another. A second owner inside the same
-- organization exists so "owner-scoped" can be distinguished from
-- "organization-scoped".
--
-- billing_owner_id stays NULL: it is FK-bound to auth.users and this file needs
-- no billing identity, only a tenant boundary.
insert into public.organizations(id, name, legacy_owner_id) values
    ('37000000-0000-4000-8000-000000000037', 'Admin account view tenant', 'admin-view-owner');

insert into public.api_keys(key_hash, name, owner_id, organization_id, key_type) values
    ('admin-view-org-key', 'Admin view org key',
     'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', '37000000-0000-4000-8000-000000000037', 'legacy'),
    ('admin-view-orgless-key', 'Admin view org-less key',
     'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', null, 'legacy');

insert into public.usage_log(
    key_hash, owner_id, organization_id, ts, request_id, project, pipeline, agent,
    baseline_tokens, optimized_tokens, tokens_saved, pricing_status,
    baseline_cost_usd, actual_cost_usd, measured_savings_usd,
    verified_savings_usd, brevitas_fee_usd, authoritative, receipt_source
) values
    -- NEWEST. Org-scoped: invisible to post-202607280035 usage_page at the
    -- admin scope, and therefore the row the admin view was about to lose.
    ('admin-view-org-key', 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037',
     '37000000-0000-4000-8000-000000000037', '2026-03-01 12:00:00+00',
     'admin-view-org-row', 'admin-org', 'admin-pipeline', 'admin-agent',
     1000, 400, 600, 'priced', 70, 30, 40, 40, 10, true, 'proxy'),
    -- Org-scoped AND written by the org-less key (the self-scope shape
    -- 202607280035 deliberately left alone). Still the account's traffic.
    ('admin-view-orgless-key', 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037',
     '37000000-0000-4000-8000-000000000037', '2026-03-01 11:00:00+00',
     'admin-view-self-row', 'admin-self', 'admin-pipeline', 'admin-agent',
     10, 4, 6, 'priced', 0.7, 0.3, 0.4, 0.4, 0.1, false, 'proxy'),
    -- OLDEST. Org-less: the only row post-202607280035 usage_page still
    -- returns at the admin scope. If the admin view were left on usage_page,
    -- this stale row would be the WHOLE account history staff see.
    ('admin-view-orgless-key', 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037',
     null, '2026-03-01 10:00:00+00',
     'admin-view-free-row', 'admin-free', 'admin-pipeline', 'admin-agent',
     100, 40, 60, 'priced', 7, 3, 4, 4, 1, false, 'proxy'),
    -- A DIFFERENT owner in the same organization. The admin read is scoped to
    -- ONE account, so no assertion below may ever surface this.
    ('admin-view-org-key', 'ffffffff-ffff-4fff-8fff-fffffffff037',
     '37000000-0000-4000-8000-000000000037', '2026-03-01 13:00:00+00',
     'admin-view-stranger-row', 'admin-stranger', 'admin-pipeline', 'admin-agent',
     5000, 2000, 3000, 'priced', 350, 150, 200, 200, 50, true, 'proxy');

-- Section 2 -- THE REGRESSION PROOF.
--
-- pg_temp.usage_page_pre_0035 is 202607170006_database_scaling.sql:59-70
-- verbatim (its predicate is the one 202607280035's own header quotes as THE
-- HOLE); only the schema and the language-level hardening differ, because this
-- is a throwaway comparison oracle and not a security surface. It is what the
-- platform-admin route was reading BEFORE the fix.
create or replace function pg_temp.usage_page_pre_0035(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text,
    p_cursor_ts timestamptz,
    p_cursor_id bigint,
    p_limit integer
) returns setof public.usage_log
language sql stable as $$
    select usage.*
    from public.usage_log usage
    where (
        (p_organization_id is not null and usage.organization_id = p_organization_id)
        or (p_organization_id is null and p_owner_id <> ''
            and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
        or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
    )
      and (p_cursor_ts is null or (usage.ts, usage.id) < (p_cursor_ts, p_cursor_id))
    order by usage.ts desc, usage.id desc
    limit least(greatest(coalesce(p_limit, 100), 1), 200) + 1;
$$;

do $$
declare
    v_before text[];
    v_after_admin text[];
    v_after_usage_page text[];
begin
    -- (a) What the platform-admin route saw BEFORE 202607280035, at the exact
    --     scope api/store.py:4676-4681 sends: empty key hash, no organization,
    --     the account id.
    select array_agg(page.request_id order by page.ts desc, page.id desc)
      into v_before
      from pg_temp.usage_page_pre_0035(
        '', null, 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', null, null, 200) page;

    -- (b) What it sees AFTER, through the dedicated read 202607280037 adds.
    select array_agg(page.request_id order by page.ts desc, page.id desc)
      into v_after_admin
      from public.admin_account_usage_page(
        'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', null, null, 200) page;

    -- THE ASSERTION THIS FILE EXISTS FOR: identical, in identical order.
    if v_after_admin is distinct from v_before then
        raise exception
            '202607280035 changed the platform-admin account view: before % / after %',
            v_before, v_after_admin;
    end if;

    -- Guard against a vacuous pass: the fixture must actually contain the
    -- mixed shape production has, or (b) = (a) proves nothing.
    if v_before is distinct from array[
        'admin-view-org-row', 'admin-view-self-row', 'admin-view-free-row'] then
        raise exception 'the admin-view fixture no longer exercises the mixed org/org-less shape: %',
            v_before;
    end if;

    -- (c) And the regression is REAL: the live post-202607280035 usage_page,
    --     at that same admin scope, now returns strictly fewer rows. If
    --     202607280035 were not applied this returns the full set and fires --
    --     which is what stops (b) from passing for the wrong reason.
    select array_agg(page.request_id order by page.ts desc, page.id desc)
      into v_after_usage_page
      from public.usage_page(
        '', null, 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', null, null, 200) page;
    if v_after_usage_page is distinct from array['admin-view-free-row'] then
        raise exception
            'usage_page at the admin scope returned % -- this file is measuring nothing',
            v_after_usage_page;
    end if;
    if coalesce(array_length(v_after_usage_page, 1), 0)
       >= coalesce(array_length(v_before, 1), 0) then
        raise exception 'usage_page did not narrow at the admin scope; the premise is gone';
    end if;

    -- The failure mode measured on production is the DANGEROUS one: the row
    -- COUNT can be unchanged while the population is different. Assert on the
    -- identities above, never on the count -- and say so here so a future edit
    -- does not "simplify" this into a count comparison.
    if array_length(v_before, 1) is null then
        raise exception 'the admin-view fixture is empty';
    end if;
end;
$$;

-- Section 3 -- the admin read is scoped to ONE account, and fails closed
-- without one. This is the whole reason it is safe to hand it cross-tenant
-- reach: it can never be turned into a platform-wide usage_log dump the way
-- admin_usage_report({}) can (202607280032:37).
do $$
declare
    v_rows text[];
begin
    if exists (
        select 1 from public.admin_account_usage_page(
            'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037', null, null, 200) page
         where page.owner_id <> 'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037'
    ) then
        raise exception 'the platform-admin account read crossed into another account';
    end if;

    select array_agg(page.request_id order by page.request_id)
      into v_rows
      from public.admin_account_usage_page(
        'ffffffff-ffff-4fff-8fff-fffffffff037', null, null, 200) page;
    if v_rows is distinct from array['admin-view-stranger-row'] then
        raise exception 'the platform-admin account read is not owner-scoped: %', v_rows;
    end if;

    -- Empty account id must RAISE, not scan. usage_page's branch 3 answers an
    -- empty owner by matching on key_hash instead; there is no key_hash here,
    -- so an unguarded empty owner would have matched the '' -owner rows of
    -- every tenant at once.
    begin
        perform * from public.admin_account_usage_page('', null, null, 200);
        raise exception 'the platform-admin account read accepted an empty account id';
    exception
        when invalid_parameter_value then null;
    end;
    begin
        perform * from public.admin_account_usage_page(null, null, null, 200);
        raise exception 'the platform-admin account read accepted a null account id';
    exception
        when invalid_parameter_value then null;
    end;
end;
$$;

-- Section 4 -- the cursor walk api/store.py's _collect_usage_rows drives
-- (api/store.py:4616-4631) must cover the same set. The walker asks for
-- p_limit rows, gets p_limit + 1 back when more remain, keeps p_limit, and
-- re-enters with the last kept row as the cursor. Driven here at p_limit = 1
-- so every boundary is exercised.
do $$
declare
    v_collected text[] := array[]::text[];
    v_cursor_ts timestamptz := null;
    v_cursor_id bigint := null;
    v_page record;
    v_page_rows integer;
    v_guard integer := 0;
begin
    loop
        v_guard := v_guard + 1;
        if v_guard > 10 then
            raise exception 'the admin cursor walk did not terminate';
        end if;
        v_page_rows := 0;
        for v_page in
            select page.request_id, page.ts, page.id
              from public.admin_account_usage_page(
                'eeeeeeee-eeee-4eee-8eee-eeeeeeeee037',
                v_cursor_ts, v_cursor_id, 1) page
             order by page.ts desc, page.id desc
             limit 1
        loop
            v_page_rows := v_page_rows + 1;
            v_collected := v_collected || v_page.request_id;
            v_cursor_ts := v_page.ts;
            v_cursor_id := v_page.id;
        end loop;
        exit when v_page_rows = 0;
    end loop;

    if v_collected is distinct from array[
        'admin-view-org-row', 'admin-view-self-row', 'admin-view-free-row'] then
        raise exception 'the admin cursor walk lost or reordered rows: %', v_collected;
    end if;
end;
$$;

-- Section 5 -- the browser roles cannot call it. This function is cross-tenant
-- by design and SECURITY DEFINER, so EXECUTE is the only authorization there
-- is; 202607280032's assert_browser_role_function_privileges() is the durable
-- control and this is the local, named restatement of it.
do $$
begin
    if has_function_privilege('anon',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
    then
        raise exception 'the platform-admin account read is browser-executable';
    end if;
    if not has_function_privilege('service_role',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
    then
        raise exception 'the API service role cannot call the platform-admin account read';
    end if;
end;
$$;

rollback;
