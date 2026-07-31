-- Org-less API key sweep, part 3 of 3: EMIT the remediation, never run it.
--
-- This file is a code GENERATOR. Every mutation below exists only as a text
-- value in a result set. Nothing here changes production, and it cannot: the
-- `begin; set transaction read only;` prologue makes the server reject any
-- write in this session, so even an editing mistake fails loudly instead of
-- mutating wyfz.
--
-- Run:  supabase db query --linked -f scripts/db/orgless_key_sweep_03_emit.sql
-- or:   bash scripts/db/orgless_key_sweep.sh emit     (prints the SQL plainly)
--
-- ---------------------------------------------------------------------------
-- READ scripts/db/orgless_key_sweep.sh's runbook BEFORE applying anything.
-- The two rules that matter most:
--   1. Every emitted UPDATE is self-guarding (`... and organization_id is null`,
--      `... and revoked_at is null`, plus a 7-day silence check and a
--      provider_config check on revokes). A guard that fires reports UPDATE 0.
--      UPDATE 0 is a STOP signal -- the world moved since this file was
--      generated. Re-run parts 1 and 2 and re-emit. Never "fix" it by deleting
--      a guard.
--   2. Apply one phase at a time, inside an explicit transaction, and read the
--      rowcount before COMMIT.
-- ---------------------------------------------------------------------------

begin;
set transaction read only;

with orgless as (
    select k.* from public.api_keys k where k.organization_id is null
),
target as (
    -- organizations.legacy_owner_id, NOT organization_members. See part 1
    -- section D_TARGET: at least one owner sits on two rosters, so membership
    -- does not name a single org. legacy_owner_id is unique and was written
    -- 1:1 from owner_id by 202607170001_enterprise_tenancy.sql:127-136.
    select o.legacy_owner_id, o.id as org_id, o.name as org_name
      from public.organizations o
     where o.legacy_owner_id is not null
),
usage_by_key as (
    select u.key_hash,
           count(*)                                                   as rows_total,
           count(*) filter (where u.ts >= now() - interval '7 days')   as rows_7d,
           count(*) filter (where u.ts >= now() - interval '24 hours') as rows_24h,
           max(u.ts)                                                   as last_write
      from public.usage_log u
      join orgless k on k.key_hash = u.key_hash
     group by u.key_hash
),
replacement as (
    select k2.owner_id,
           count(*) filter (where k2.organization_id is not null
                              and k2.revoked_at is null
                              and (k2.expires_at is null or k2.expires_at > now())) as live_org_keys
      from public.api_keys k2
     group by k2.owner_id
),
keys as (
    select k.id, k.name, k.environment, left(k.key_hash, 12) as key_fp,
           left(k.owner_id, 8)                as owner_fp,
           t.org_id, t.org_name,
           coalesce(u.rows_total, 0)          as rows_total,
           coalesce(u.rows_7d, 0)             as rows_7d,
           coalesce(u.rows_24h, 0)            as rows_24h,
           coalesce(to_char(u.last_write, 'YYYY-MM-DD HH24:MI'), 'never') as last_write,
           coalesce(r.live_org_keys, 0)       as live_org_keys,
           (select count(*) from public.provider_config pc where pc.key_hash = k.key_hash) as pc_rows
      from orgless k
      left join target       t on t.legacy_owner_id = k.owner_id
      left join usage_by_key u on u.key_hash        = k.key_hash
      left join replacement  r on r.owner_id        = k.owner_id
)

-- PHASE 0 -- preflight, run and read these BEFORE any mutation ------------
select 0 as ord, 0 as sub, 'PHASE_0_PREFLIGHT' as phase,
       '-- Re-establish, at apply time, the four facts this emission assumed.' as sql
union all select 0, 1, 'PHASE_0_PREFLIGHT',
       '-- Expect: 43 org-less, 0 with provider_config, 1 active in 24h, 0 authoritative org-less usage rows.'
union all select 0, 2, 'PHASE_0_PREFLIGHT',
       'select count(*) filter (where organization_id is null) as orgless,'
    || ' count(*) filter (where organization_id is null and revoked_at is not null) as already_revoked'
    || ' from public.api_keys;'
union all select 0, 3, 'PHASE_0_PREFLIGHT',
       'select count(*) as provider_config_at_risk from public.provider_config pc'
    || ' join public.api_keys k on k.key_hash = pc.key_hash where k.organization_id is null;'
union all select 0, 4, 'PHASE_0_PREFLIGHT',
       'select count(distinct u.key_hash) as active_24h from public.usage_log u'
    || ' join public.api_keys k on k.key_hash = u.key_hash'
    || ' where k.organization_id is null and u.ts >= now() - interval ''24 hours'';'

-- PHASE 1 -- backfill organization_id on the determinate keys -------------
union all select 1, 0, 'PHASE_1_BACKFILL',
       '-- ' || (select count(*)::text from keys where org_id is not null)
    || ' keys have exactly one legacy org. Backfill is reversible: set the column back to NULL.'
union all select 1, 1, 'PHASE_1_BACKFILL',
       '-- Backfilling a KEY does not backfill its HISTORY. usage_log rows keep'
union all select 1, 2, 'PHASE_1_BACKFILL',
       '-- organization_id IS NULL, so 202607280035 still scopes those reads by owner_id.'
union all select 1, 3, 'PHASE_1_BACKFILL',
       '-- Rewriting usage_log is billing evidence and is NOT part of this sweep.'
union all
select 1, 10 + row_number() over (order by k.org_id, k.key_fp), 'PHASE_1_BACKFILL',
       'update public.api_keys set organization_id = ' || quote_literal(k.org_id::text) || '::uuid'
    || ' where id = ' || quote_literal(k.id::text) || '::uuid and organization_id is null;'
    || '  -- ' || k.key_fp || ' owner=' || k.owner_fp || ' -> ' || replace(replace(k.org_name, E'\n', ' '), E'\r', ' ')
    || ' (' || k.rows_total::text || ' usage rows, last write ' || k.last_write || ')'
  from keys k
 where k.org_id is not null

-- PHASE 2 -- OPTIONAL: de-duplicate names before a future rebuild ---------
union all select 2, 0, 'PHASE_2_RENAME_OPTIONAL',
       '-- Only needed if you ever intend to rebuild this database from the'
union all select 2, 1, 'PHASE_2_RENAME_OPTIONAL',
       '-- migration chain. Prod''s api_keys predates 202607170001 and never got'
union all select 2, 2, 'PHASE_2_RENAME_OPTIONAL',
       '-- its table constraints, but a FRESH schema declares'
union all select 2, 3, 'PHASE_2_RENAME_OPTIONAL',
       '-- unique (organization_id, name, environment) (202607170001:98). After'
union all select 2, 4, 'PHASE_2_RENAME_OPTIONAL',
       '-- phase 1 the names below collide, so the rebuild would fail. Renaming is'
union all select 2, 5, 'PHASE_2_RENAME_OPTIONAL',
       '-- cosmetic on prod and shows up in the dashboard key list -- tell the owner first.'
union all
select 2, 10 + row_number() over (order by k.org_id, k.key_fp), 'PHASE_2_RENAME_OPTIONAL',
       'update public.api_keys set name = ' || quote_literal(k.name || ' (' || k.key_fp || ')')
    || ' where id = ' || quote_literal(k.id::text) || '::uuid and name = ' || quote_literal(k.name) || ';'
    || '  -- ' || k.key_fp || ' in ' || replace(replace(k.org_name, E'\n', ' '), E'\r', ' ')
  from keys k
 where k.org_id is not null
   and exists (
        select 1 from keys d
         where d.org_id = k.org_id and d.name = k.name and d.environment = k.environment
           and d.id <> k.id)

-- PHASE 3 -- revoke, only where revoking is provably safe -----------------
union all select 3, 0, 'PHASE_3_REVOKE',
       '-- REVOKE IS NOT REVERSIBLE IN EFFECT. Setting revoked_at fires trigger'
union all select 3, 1, 'PHASE_3_REVOKE',
       '-- api_keys_provider_config_cleanup (202607200008_provider_credential_cleanup.sql:104-107),'
union all select 3, 2, 'PHASE_3_REVOKE',
       '-- which DELETEs public.provider_config for that key_hash: the customer''s'
union all select 3, 3, 'PHASE_3_REVOKE',
       '-- provider credential and model. Clearing revoked_at afterwards does not bring it back.'
union all select 3, 4, 'PHASE_3_REVOKE',
       '-- Each statement below refuses to fire if the key has provider_config or has'
union all select 3, 5, 'PHASE_3_REVOKE',
       '-- written usage in the last 7 days. UPDATE 0 means STOP and re-emit.'
union all
select 3, 10 + row_number() over (order by k.rows_total, k.key_fp), 'PHASE_3_REVOKE',
       'update public.api_keys k set revoked_at = now() where k.id = ' || quote_literal(k.id::text) || '::uuid'
    || ' and k.revoked_at is null'
    || ' and not exists (select 1 from public.provider_config pc where pc.key_hash = k.key_hash)'
    || ' and not exists (select 1 from public.usage_log u where u.key_hash = k.key_hash'
    || ' and u.ts >= now() - interval ''7 days'');'
    || '  -- ' || k.key_fp || ' owner=' || k.owner_fp || ' last write ' || k.last_write
    || ', ' || k.rows_total::text || ' lifetime rows'
  from keys k
 where k.rows_7d = 0
   and k.pc_rows = 0
   and (k.org_id is not null or k.rows_total = 0)

-- PHASE 4 -- HOLDS: emitted as comments only. Do not uncomment blindly. ---
union all select 4, 0, 'PHASE_4_HOLD',
       '-- These keys are deliberately NOT given a revoke statement.'
union all
select 4, 10 + row_number() over (order by k.rows_24h desc, k.rows_total desc, k.key_fp), 'PHASE_4_HOLD',
       '-- HOLD ' || k.key_fp || ' owner=' || k.owner_fp
    || ' [' || case
                 when k.rows_24h > 0 then 'LIVE: ' || k.rows_24h::text || ' usage rows in the last 24h'
                 when k.rows_7d  > 0 then 'RECENT: ' || k.rows_7d::text || ' usage rows in the last 7d'
                 when k.pc_rows  > 0 then 'HAS provider_config: revoking destroys the credential'
                 else 'ORPHAN: ' || k.rows_total::text || ' historical rows and no organizations row for this owner'
               end
    || '] last write ' || k.last_write
    || '; owner has ' || k.live_org_keys::text || ' live org-bearing key(s)'
    || case when k.live_org_keys = 0 then ' -- revoking this owner is a full outage for them' else '' end
  from keys k
 where not (k.rows_7d = 0 and k.pc_rows = 0 and (k.org_id is not null or k.rows_total = 0))

-- PHASE 5 -- postconditions ----------------------------------------------
union all select 5, 0, 'PHASE_5_VERIFY',
       'select count(*) as orgless_remaining from public.api_keys where organization_id is null;'
union all select 5, 1, 'PHASE_5_VERIFY',
       'select count(*) as orgless_still_live from public.api_keys'
    || ' where organization_id is null and revoked_at is null;'
union all select 5, 2, 'PHASE_5_VERIFY',
       'select count(*) as provider_config_lost from public.api_keys k'
    || ' where k.revoked_at is not null and not exists'
    || ' (select 1 from public.provider_config pc where pc.key_hash = k.key_hash);'
    || '  -- compare against the phase 0 reading; any increase is destroyed credentials'
union all select 5, 3, 'PHASE_5_VERIFY',
       '-- Then re-run scripts/db/orgless_key_sweep_01_summary.sql and diff sections A, D and E.'

order by ord, sub;

commit;
