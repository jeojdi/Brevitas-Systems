-- Org-less API key sweep, part 1 of 3: cohort shape, exposure, traps.
--
-- READ-ONLY BY CONSTRUCTION -- see scripts/db/orgless_key_sweep.sh for the
-- runbook. The `begin; set transaction read only;` prologue is server-enforced.
--
-- Run:  supabase db query --linked -f scripts/db/orgless_key_sweep_01_summary.sql
--
-- Answers, in order: is the cohort closed? is the cross-tenant read real or
-- latent? is the backfill target single-valued? what does a revoke destroy?
-- what will bite you afterwards?

begin;
set transaction read only;

with orgless as (
    select k.* from public.api_keys k where k.organization_id is null
),
target as (
    select o.legacy_owner_id, o.id as org_id, o.name as org_name
      from public.organizations o
     where o.legacy_owner_id is not null
)

-- A. COHORT ---------------------------------------------------------------
select 10 as ord, 'A_COHORT' as section, 'api_keys total' as item,
       count(*)::text as detail from public.api_keys
union all
select 11, 'A_COHORT', 'organization_id IS NULL', count(*)::text from orgless
union all
select 12, 'A_COHORT', 'distinct key_type among the org-less',
       coalesce(string_agg(distinct key_type, ','), '(none)') from orgless
union all
select 13, 'A_COHORT', 'still reachable (revoked_at IS NULL and not expired)',
       count(*)::text from orgless
 where revoked_at is null and (expires_at is null or expires_at > now())
union all
select 14, 'A_COHORT', 'distinct scope sets (1 = the route scope gate stops none of them)',
       count(*)::text from (select distinct scopes from orgless) s
union all
select 15, 'A_COHORT', 'distinct owner_id', count(distinct owner_id)::text from orgless
union all
select 16, 'A_COHORT', 'owner_id = '''' (unattributable by design, 202607170001:137)',
       count(*)::text from orgless where owner_id = ''
union all
select 17, 'A_COHORT', 'attached to a service_account', count(*)::text
  from orgless where service_account_id is not null

-- B. IS THE COHORT CLOSED? ------------------------------------------------
union all
select 20, 'B_CLOSED', 'newest org-less key was minted',
       coalesce(to_char(max(created), 'YYYY-MM-DD'), 'n/a') from orgless
union all
select 21, 'B_CLOSED', 'oldest org-less key was minted',
       coalesce(to_char(min(created), 'YYYY-MM-DD'), 'n/a') from orgless
union all
select 22, 'B_CLOSED', 'org-BEARING keys minted since that date',
       count(*)::text from public.api_keys
 where organization_id is not null
   and created > (select max(created) from orgless)
union all
select 23, 'B_CLOSED', 'org-less keys minted in the last 7 days (>0 means a live mint path still exists)',
       count(*)::text from orgless where created >= now() - interval '7 days'

-- C. EXPOSURE: is the 202607280035 hole open, and is the leak realised? ----
union all
select 30, 'C_EXPOSURE', '202607280035 applied here? (usage_page carries the narrowed predicate)',
       coalesce((
         select (pg_get_functiondef(p.oid)
                 like '%(usage.organization_id is null and usage.owner_id = p_owner_id)%')::text
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
          where n.nspname = 'public' and p.proname = 'usage_page'
          limit 1), 'usage_page NOT FOUND')
union all
select 31, 'C_EXPOSURE',
       'owner ' || left(k.owner_id, 8) || ' (' || count(distinct k.key_hash)::text || ' org-less keys) can read org rows',
       (select count(*) from public.usage_log u
         where u.owner_id = k.owner_id and u.organization_id is not null)::text
       || ' rows; of those, in an org that is NOT this owner''s own legacy org: '
       || (select count(*) from public.usage_log u
             left join target t on t.legacy_owner_id = k.owner_id
            where u.owner_id = k.owner_id
              and u.organization_id is not null
              and (t.org_id is null or u.organization_id <> t.org_id))::text
  from orgless k
 group by k.owner_id
having (select count(*) from public.usage_log u
         where u.owner_id = k.owner_id and u.organization_id is not null) > 0

-- D. BACKFILL DETERMINACY -------------------------------------------------
union all
select 40, 'D_TARGET', 'org-less keys WITH a legacy org (backfillable)',
       count(*)::text from orgless k
 where exists (select 1 from target t where t.legacy_owner_id = k.owner_id)
union all
select 41, 'D_TARGET', 'org-less keys with NO organizations row for their owner (nothing to backfill; do NOT create orgs)',
       count(*)::text from orgless k
 where not exists (select 1 from target t where t.legacy_owner_id = k.owner_id)
union all
select 42, 'D_TARGET', 'legacy_owner_id values mapping to >1 org (must be 0 -- unique index)',
       (select count(*)::text from (
          select legacy_owner_id from public.organizations
           where legacy_owner_id is not null
           group by legacy_owner_id having count(*) > 1) d)
union all
-- The trap that makes organization_members the WRONG key: an owner can sit on
-- several rosters, so membership does not identify a single target.
select 43, 'D_TARGET',
       'owner ' || left(k.owner_id, 8) || ' sits on N org rosters (>1 => membership-driven backfill would be ambiguous)',
       (select count(*) from public.organization_members m
         where m.user_id::text = k.owner_id and m.status = 'active')::text
       || ' active, '
       || (select count(*) from public.organization_members m
            where m.user_id::text = k.owner_id and m.status <> 'active')::text
       || ' disabled/removed; legacy_owner_id resolves to: '
       || coalesce((select left(t.org_id::text, 8) from target t where t.legacy_owner_id = k.owner_id), 'NO_ORG')
  from orgless k
 group by k.owner_id
having (select count(*) from public.organization_members m where m.user_id::text = k.owner_id) > 1
union all
-- The condition that turns the latent leak into a real one: the human lost
-- their seat, but the org-less key never consulted the roster in the first
-- place, so it keeps reading.
select 44, 'D_TARGET',
       'org-less key owners whose membership in their own legacy org is NOT active (leak becomes real here)',
       (select count(distinct k.owner_id)::text
          from orgless k
          join target t on t.legacy_owner_id = k.owner_id
         where not exists (
             select 1 from public.organization_members m
              where m.organization_id = t.org_id and m.user_id::text = k.owner_id
                and m.status = 'active'))

-- E. BLAST RADIUS OF REVOKE ----------------------------------------------
union all
select 50, 'E_REVOKE',
       'provider_config rows the revoke trigger would DELETE (202607200008:104-107)',
       (select count(*) from public.provider_config pc
         where pc.key_hash in (select key_hash from orgless))::text
       || ' -- irreversible; these are the customer''s provider credential + model'
union all
select 51, 'E_REVOKE', 'ai_jobs rows',
       (select count(*) from public.ai_jobs j where j.key_hash in (select key_hash from orgless))::text
union all
select 52, 'E_REVOKE', 'audit_events (actor_key_hash) rows',
       (select count(*) from public.audit_events a where a.actor_key_hash in (select key_hash from orgless))::text
union all
select 53, 'E_REVOKE', 'bvx_device_auth rows',
       (select count(*) from public.bvx_device_auth d where d.key_hash in (select key_hash from orgless))::text
union all
select 54, 'E_REVOKE', 'bvx_device_consumption_receipts rows',
       (select count(*) from public.bvx_device_consumption_receipts c where c.key_hash in (select key_hash from orgless))::text
union all
select 55, 'E_REVOKE', 'installations (registration_key_hash) rows',
       (select count(*) from public.installations i where i.registration_key_hash in (select key_hash from orgless))::text
union all
select 56, 'E_REVOKE', 'key_repositories rows',
       (select count(*) from public.key_repositories kr where kr.key_hash in (select key_hash from orgless))::text
union all
select 57, 'E_REVOKE', 'usage_log rows written by these keys',
       (select count(*) from public.usage_log u where u.key_hash in (select key_hash from orgless))::text
union all
select 58, 'E_REVOKE', 'org-less keys that wrote usage in the last 24h (revoking one of these is an outage)',
       (select count(distinct u.key_hash) from public.usage_log u
         where u.key_hash in (select key_hash from orgless)
           and u.ts >= now() - interval '24 hours')::text
union all
select 59, 'E_REVOKE', 'org-less keys that have NEVER written a usage row',
       (select count(*) from orgless k
         where not exists (select 1 from public.usage_log u where u.key_hash = k.key_hash))::text

-- F. TRAPS THAT SURVIVE THE SWEEP ----------------------------------------
union all
-- Prod's api_keys predates 202607170001, so it was reached only by the
-- `alter table ... add column` half of that file; the table constraints in the
-- `create table if not exists` half were never applied here. A FRESH database
-- gets `unique (organization_id, name, environment)`
-- (202607170001_enterprise_tenancy.sql:98). Backfilling org-less keys that
-- share a name would be legal on prod and ILLEGAL on a rebuilt database.
select 60, 'F_TRAPS',
       'unique(organization_id,name,environment) present on this database? (fresh schema HAS it)',
       (exists (
          select 1 from pg_constraint
           where conrelid = 'public.api_keys'::regclass and contype = 'u'
             and pg_get_constraintdef(oid) = 'UNIQUE (organization_id, name, environment)'))::text
union all
select 61, 'F_TRAPS',
       'post-backfill duplicate (org, name, environment): org ' || left(t.org_id::text, 8)
       || ' name=' || k.name || ' env=''' || k.environment || '''',
       count(*)::text || ' keys would collide -- harmless on prod today, fatal on a rebuild'
  from orgless k join target t on t.legacy_owner_id = k.owner_id
 group by t.org_id, k.name, k.environment
having count(*) > 1
union all
select 62, 'F_TRAPS',
       'usage_log rows still carrying organization_id IS NULL (backfilling KEYS does not backfill HISTORY)',
       (select count(*) from public.usage_log where organization_id is null)::text
union all
select 63, 'F_TRAPS',
       'of those, rows with authoritative = true (billable evidence -- must not be touched casually)',
       (select count(*) from public.usage_log where organization_id is null and authoritative)::text

order by ord, item;

commit;
