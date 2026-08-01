-- REVERSE: DDL: alter table public.organizations alter column cache_enabled set default false
--
-- New organizations get caching ON. Existing rows are not touched.
--
-- WHY. public.organizations.cache_enabled has defaulted FALSE since
-- 202607170001, and NOTHING in the product ever sets it: not signup, not
-- ensure_workspace_organization (202607200018), not company administration
-- (202607170005) -- verified, zero mentions of the column in any of them. The
-- only writer is PUT /v1/cache-policy (api/server.py), which no dashboard or web
-- code calls. The five organizations on production read TRUE only because a
-- human ran SQL by hand.
--
-- The consequence is the whole business, silently: brevitas/proxy.py's
-- _cache_for_request returns None when the tenant has not opted in, so there are
-- no cache replays, so verified_savings_usd is 0 on every row, so the fee basis
-- is empty and the invoice is $0 -- forever, with no error anywhere. A company
-- could sign up, install correctly, send perfect traffic, and be billed nothing,
-- and neither side would find out until someone asked why the invoice was zero.
--
-- WHAT THIS DOES NOT CHANGE, deliberately:
--   * Existing rows. ALTER COLUMN ... SET DEFAULT applies to future INSERTs only.
--     Nobody's current setting moves, including anyone who turned it off on
--     purpose.
--   * public.customers.cache_enabled stays FALSE by default. The resolution in
--     api/store.py's cache_enabled() is an OR, not an AND -- a customer row can
--     only raise caching to on, never gate it off -- so the organization default
--     is sufficient and touching the customer default would add nothing.
--   * The off switch. PUT /v1/cache-policy still turns caching off per
--     organization or per customer, and still answers {"purged": true}.
--
-- ON CONSENT. _cache_enabled_cached's contract says failing to False "can never
-- serve a cached answer the tenant did not consent to", and that reasoning is
-- why the default was FALSE. It is still right about the failure path, which is
-- unchanged: a store error still means not-cached. What changes is the starting
-- position for someone who signed up for a caching product. Signing up IS the
-- consent; an opt-in that the product provides no way to exercise is not
-- consent, it is a dead switch. The dashboard toggle that makes this a real
-- choice is the necessary companion to this migration and is not in it.

begin;

do $$
begin
    if to_regclass('public.organizations') is null then
        raise exception '202607280038 requires public.organizations from 202607170001'
            using errcode = '55000';
    end if;
end
$$;

alter table public.organizations
    alter column cache_enabled set default true;

comment on column public.organizations.cache_enabled is
    'Whether Brevitas may serve cache replays for this organization. Defaults '
    'TRUE for organizations created from 202607280038 onward: the product is the '
    'cache, and a FALSE default that nothing in signup ever flipped meant a new '
    'customer accrued zero savings and a $0 invoice with no error. Rows created '
    'before that migration keep whatever they were set to. Turn it off with '
    'PUT /v1/cache-policy. api/store.py resolves it as customer OR organization, '
    'so a customer row can raise this to on but never gate it off.';

do $contract$
declare
    v_default text;
begin
    select column_default into v_default
      from information_schema.columns
     where table_schema = 'public'
       and table_name = 'organizations'
       and column_name = 'cache_enabled';

    if v_default is null or v_default not like '%true%' then
        raise exception
            '202607280038 did not set the cache_enabled default: %',
            coalesce(v_default, '(null)')
            using errcode = '55000';
    end if;

    -- The column must stay NOT NULL: a NULL would make the OR in
    -- api/store.py's cache_enabled() resolve to false and silently disable
    -- caching for the org, which is the exact failure this migration exists to
    -- remove.
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'organizations'
           and column_name = 'cache_enabled' and is_nullable = 'YES'
    ) then
        raise exception '202607280038 left organizations.cache_enabled nullable'
            using errcode = '55000';
    end if;
end
$contract$;

commit;
