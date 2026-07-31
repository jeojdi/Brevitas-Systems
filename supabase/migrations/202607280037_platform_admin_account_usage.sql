-- REVERSE: DDL: drop function public.admin_account_usage_page(text,timestamptz,bigint,integer)
--
-- Give the platform-admin account view its own cross-tenant read, so
-- 202607280035 can be applied without silently rewriting what staff see.
--
-- ============================================================================
-- WHY THIS EXISTS: 202607280035 IS CORRECT AND UNAPPLYABLE
-- ============================================================================
--
-- 202607280035 narrowed the ORG-LESS branch of public.usage_page so that a key
-- carrying no organization can no longer read org-owned rows that merely share
-- its owner_id string. That is the right fix for the key-authenticated read
-- path, and nothing here loosens it.
--
-- But one caller is not a key. api/store.py:4676-4681
-- (SupabaseUsageStore.get_admin_account_detail, serving
-- GET /v1/admin/accounts/{owner_id}/usage) reaches usage_page with
--
--     {"p_key_hash": "", "p_organization_id": None, "p_owner_id": owner_id}
--
-- which is EXACTLY the branch 202607280035 narrowed. That caller is not a
-- tenant asking for its own rows; it is a platform administrator asking "show
-- me everything this account did", already gated on
-- api/server.py:2217-2223 (_admin_authenticated: app_metadata.brevitas_admin,
-- validated server-side against GoTrue, never read from the client) and
-- already audited fail-closed on api/server.py:2225-2256 (_audit_platform_read
-- returns 503 and the read never happens if the audit_events append raises).
--
-- MEASURED ON PRODUCTION (wyfz, SELECT-only, 2026-07-31), what applying
-- 202607280035 would have done to that view:
--
--   owner_id      total   org-less   org-scoped   of the NEWEST 1000, org-scoped
--   1f6ea90d-..   7,155      5,612        1,543          1000 / 1000
--   c7e2782f-..   7,009      1,117        5,892          1000 / 1000
--   ba547a01-..   2,440      2,398           42            42 / 1000
--   (4 others)   28,401     28,401            0             0
--   (owner_id '')     2          2            0             0
--
-- (45,007 rows total.)
--
-- api/store.py:4616-4631 walks the usage_page cursor only up to
-- ACTIVITY_SCAN_MAX = 1000 rows (api/store.py:1306). For the first two owners
-- every one of the newest 1000 rows is org-scoped, so post-202607280035 the
-- admin view would still report window_calls = 1000 -- but computed over a
-- COMPLETELY DIFFERENT, OLDER population. No count changes, no error is
-- raised, and the totals and activity blocks quietly describe stale history.
-- A visible drop would have been safer than that.
--
-- Also measured: `select count(*) from usage_log where key_hash = ''` is 0, so
-- the empty p_key_hash matched nothing and the pre-202607280035 admin row set
-- was exactly `owner_id = p_owner_id`, nothing more. That is precisely what
-- the function below returns -- which is what makes this a no-op for staff and
-- lets 202607280035 land untouched.
--
-- And 2 production rows carry owner_id = '' -- which is why the function below
-- REFUSES an empty account id instead of scanning: an unguarded empty owner
-- would have returned those rows under whatever account the route was asked
-- about.
--
-- ============================================================================
-- THIS IS NOT A NEW CLASS OF ACCESS
-- ============================================================================
--
-- public.admin_usage_report(jsonb,integer) and
-- public.admin_usage_report_page(...) (202607170006_database_scaling.sql:258
-- and :355) are ALREADY cross-tenant: they filter on p_filters->>'owner_id'
-- with no tenant predicate at all, are service_role-only
-- (202607170006:490-500), and are called from api/store.py:4794 and :4825
-- behind the same _admin_authenticated gate. 202607280032_browser_function_execute_contract.sql:37
-- names `admin_usage_report({})` in as many words as "cross-tenant owner_id,
-- revenue and savings dump".
--
-- So this migration does not create cross-tenant reach. It MOVES ONE CALLER
-- off a tenant-scoped predicate it was abusing, into the function family where
-- that reach is already declared, already service_role-only, and already
-- audited -- and in doing so it removes the last reason 202607280035 could not
-- be applied.
--
-- ============================================================================
-- THE CONTRACT
-- ============================================================================
--
--   * owner-scoped, never platform-wide. An empty or null p_owner_id RAISES
--     rather than falling through to an unfiltered scan, so a caller bug
--     cannot turn this into a full usage_log dump. (usage_page's own branch 3
--     treats an empty owner as "match by key_hash instead"; there is no
--     key_hash here, so there is nothing to fall back TO.)
--   * cursor-paginated on (ts desc, id desc), returning p_limit + 1 rows, so
--     api/store.py's existing _collect_usage_rows walker drives it unchanged.
--     Backed by usage_log_owner_page_idx (202607170006:30-31).
--   * SECURITY DEFINER + service_role only. Revoked from public, anon and
--     authenticated inline per 202607280032's contract, and asserted below --
--     202607280032 also dropped the pg_default_acl grant, so nothing
--     auto-grants this to the browser roles, but the revoke is stated anyway
--     because this migration sits AFTER 202607280032's one-time pg_proc sweep.
--
-- NOT A DATA CHANGE: no row is inserted, updated or deleted, and no existing
-- function is replaced. Rollback is dropping the new function, which is what
-- the header above declares; the admin route would then have to go back to
-- usage_page, i.e. 202607280035 would have to come out with it.

begin;

do $$
begin
    if to_regclass('public.usage_log') is null then
        raise exception '202607280037 requires public.usage_log'
            using errcode = '55000';
    end if;
    -- Same guard 202607280035 uses: the return type is `setof
    -- public.usage_log`, so the column set this function hands back is
    -- whatever the table has. Requiring 202607280002's widest column proves
    -- the cache-metrics columns are present, i.e. that the admin view and the
    -- tenant view are projecting the same shape.
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'usage_log'
          and column_name = 'provider_input_tokens_avoided'
    ) then
        raise exception
            '202607280037 requires 202607280002_usage_stats_cache_metrics first'
            using errcode = '55000';
    end if;
end;
$$;

create or replace function public.admin_account_usage_page(
    p_owner_id text,
    p_cursor_ts timestamptz,
    p_cursor_id bigint,
    p_limit integer
) returns setof public.usage_log
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
    -- Fail closed on a missing account id. Every other predicate in this
    -- function is `owner_id = p_owner_id`; without this an empty string would
    -- quietly select the rows whose owner_id is '' -- 202607280026-era
    -- unattributed traffic -- under the name of a specific account.
    if coalesce(p_owner_id, '') = '' then
        raise exception 'admin_account_usage_page requires an account id'
            using errcode = '22023';
    end if;

    return query
    select usage.*
    from public.usage_log usage
    where usage.owner_id = p_owner_id
      and (p_cursor_ts is null or (usage.ts, usage.id) < (p_cursor_ts, p_cursor_id))
    order by usage.ts desc, usage.id desc
    limit least(greatest(coalesce(p_limit, 100), 1), 200) + 1;
end;
$$;

revoke all on function public.admin_account_usage_page(text,timestamptz,bigint,integer)
    from public, anon, authenticated;
grant execute on function public.admin_account_usage_page(text,timestamptz,bigint,integer)
    to service_role;

-- Apply-time self-check, catalog-only. Proves the name resolves to exactly one
-- function (a retyped signature would have created an OVERLOAD beside this one
-- and PostgREST would pick by argument names, not by the one this migration
-- reasoned about), that the owner guard and the cross-tenant posture are both
-- actually in the installed body, and that only service_role can call it.
do $contract$
declare
    v_src text;
begin
    if (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = 'admin_account_usage_page') <> 1 then
        raise exception
            '202607280037 left more than one public.admin_account_usage_page -- an overload, not a definition'
            using errcode = '55000';
    end if;

    select p.prosrc into v_src
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public' and p.proname = 'admin_account_usage_page';

    if v_src not like '%requires an account id%' then
        raise exception 'admin_account_usage_page would scan on an empty account id'
            using errcode = '55000';
    end if;
    -- The whole point of this function is that it does NOT carry
    -- 202607280035's tenant predicate: staff must keep seeing org-scoped rows.
    -- If a future edit copies that predicate in here, the admin view silently
    -- regresses to the stale-population failure this migration exists to
    -- prevent, so refuse it at apply time.
    if v_src like '%organization_id%' then
        raise exception
            'admin_account_usage_page acquired an organization predicate -- it is the platform-admin read and must stay owner-scoped only'
            using errcode = '55000';
    end if;

    if not exists (
        select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = 'admin_account_usage_page'
           and p.prosecdef
    ) then
        raise exception 'admin_account_usage_page is not SECURITY DEFINER'
            using errcode = '55000';
    end if;

    if has_function_privilege('anon',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
    then
        raise exception 'the platform-admin account read is browser-executable'
            using errcode = '42501';
    end if;

    if not has_function_privilege('service_role',
        'public.admin_account_usage_page(text,timestamptz,bigint,integer)', 'execute')
    then
        raise exception 'the API service role cannot call the platform-admin account read'
            using errcode = '42501';
    end if;
end
$contract$;

commit;
