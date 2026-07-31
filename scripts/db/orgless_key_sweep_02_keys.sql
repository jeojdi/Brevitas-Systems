-- Org-less API key sweep, part 2 of 3: ONE ROW PER KEY, the decision table.
--
-- READ-ONLY BY CONSTRUCTION. See scripts/db/orgless_key_sweep.sh for the
-- runbook. The `begin; set transaction read only;` prologue is enforced by the
-- server, not by convention: any write in this file would abort with
-- "cannot execute UPDATE in a read-only transaction".
--
-- Run:  supabase db query --linked -f scripts/db/orgless_key_sweep_02_keys.sql
--
-- Deliberately prints only left(key_hash, 12) as `key_fp`. The full key_hash is
-- never emitted anywhere in this sweep; remediation statements address rows by
-- api_keys.id (unique, NOT NULL -- api_keys_id_idx) instead.

begin;
set transaction read only;

with orgless as (
    select k.*
      from public.api_keys k
     where k.organization_id is null
),
-- The ONLY admissible backfill target. Not organization_members: an owner can
-- belong to several orgs (one owner here is an active member of two), which
-- makes a membership-driven target ambiguous. organizations.legacy_owner_id is
-- UNIQUE and was populated 1:1 from api_keys.owner_id by
-- 202607170001_enterprise_tenancy.sql:127-136, so it is single-valued.
target as (
    select o.legacy_owner_id,
           o.id           as org_id,
           o.name         as org_name,
           o.billing_owner_id
      from public.organizations o
     where o.legacy_owner_id is not null
),
usage_by_key as (
    select u.key_hash,
           count(*)                                                          as rows_total,
           count(*) filter (where u.ts >= now() - interval '7 days')          as rows_7d,
           count(*) filter (where u.ts >= now() - interval '24 hours')        as rows_24h,
           count(*) filter (where u.authoritative)                            as rows_authoritative,
           count(*) filter (where u.organization_id is not null)              as rows_already_org,
           max(u.ts)                                                          as last_write
      from public.usage_log u
      join orgless k on k.key_hash = u.key_hash
     group by u.key_hash
),
-- Does the owner have a working credential that is NOT one of the 43? If not,
-- revoking is an outage for that owner, not a cleanup.
replacement as (
    select k2.owner_id,
           count(*) filter (where k2.organization_id is not null
                              and k2.revoked_at is null
                              and (k2.expires_at is null or k2.expires_at > now())) as live_org_keys
      from public.api_keys k2
     group by k2.owner_id
)
select
    row_number() over (order by coalesce(u.rows_24h, 0) desc,
                                coalesce(u.rows_total, 0) desc,
                                k.key_hash)                       as ord,
    left(k.key_hash, 12)                                          as key_fp,
    k.id::text                                                    as key_id,
    k.name                                                        as key_name,
    left(k.owner_id, 8)                                           as owner_fp,
    to_char(k.created, 'YYYY-MM-DD')                              as created,
    coalesce(to_char(k.last_used_at, 'YYYY-MM-DD HH24:MI'), '-')   as last_used_at,
    coalesce(u.rows_total, 0)                                     as usage_rows,
    coalesce(u.rows_7d, 0)                                        as usage_7d,
    coalesce(u.rows_24h, 0)                                       as usage_24h,
    coalesce(to_char(u.last_write, 'YYYY-MM-DD HH24:MI'), 'never') as last_write,
    coalesce(u.rows_authoritative, 0)                             as authoritative_rows,
    coalesce(u.rows_already_org, 0)                               as rows_already_org,
    coalesce(left(t.org_id::text, 8), 'NO_ORG')                   as target_org,
    coalesce(t.org_name, '-')                                     as target_org_name,
    -- Is the owner still on the roster of the org we would attach the key to?
    -- Reported as role/status, not a boolean: organization_members.status is one
    -- of active|disabled|removed, and a DISABLED member is precisely the case
    -- where the org-less key keeps reading after the human lost access. A bare
    -- EXISTS would score that as "member" and hide the whole point.
    coalesce((select m.role || '/' || m.status
                from public.organization_members m
               where m.organization_id = t.org_id and m.user_id::text = k.owner_id),
             'not_a_member')                                            as owner_membership,
    -- Would attaching this key hand its rows to somebody else's bill?
    (t.billing_owner_id is not null and t.billing_owner_id::text <> k.owner_id)  as billing_owner_differs,
    coalesce(r.live_org_keys, 0)                                  as owner_live_org_keys,
    -- REVOKE DESTROYS THIS ROW. Trigger api_keys_provider_config_cleanup
    -- (202607200008_provider_credential_cleanup.sql:104-107) fires
    -- AFTER UPDATE OF revoked_at and deletes provider_config by key_hash.
    (select count(*) from public.provider_config pc where pc.key_hash = k.key_hash)                  as provider_config_rows,
    (select count(*) from public.ai_jobs j            where j.key_hash = k.key_hash)                 as ai_jobs_rows,
    (select count(*) from public.audit_events a       where a.actor_key_hash = k.key_hash)           as audit_actor_rows,
    (select count(*) from public.bvx_device_auth d    where d.key_hash = k.key_hash)                 as device_auth_rows,
    (select count(*) from public.bvx_device_consumption_receipts c where c.key_hash = k.key_hash)    as device_receipt_rows,
    (select count(*) from public.installations i      where i.registration_key_hash = k.key_hash)    as installation_rows,
    (select count(*) from public.key_repositories kr  where kr.key_hash = k.key_hash)                as key_repo_rows,
    case
        when coalesce(u.rows_24h, 0) > 0
            then 'LIVE - DO NOT REVOKE. Migrate the caller onto an org key first.'
        when coalesce(u.rows_7d, 0) > 0
            then 'RECENT - backfill only; revoke needs owner sign-off.'
        when t.org_id is null and coalesce(u.rows_total, 0) = 0
            then 'DORMANT + NO TARGET - revoke candidate.'
        when t.org_id is null
            then 'ORPHAN - has history, no org to attach to. Leave org-less; do NOT invent an org.'
        when coalesce(r.live_org_keys, 0) = 0
            then 'BACKFILL, then hold - owner has no other live org key.'
        else 'BACKFILL, then revoke after the silence window.'
    end                                                           as verdict
  from orgless k
  left join target        t on t.legacy_owner_id = k.owner_id
  left join usage_by_key  u on u.key_hash        = k.key_hash
  left join replacement   r on r.owner_id        = k.owner_id
 order by ord;

commit;
