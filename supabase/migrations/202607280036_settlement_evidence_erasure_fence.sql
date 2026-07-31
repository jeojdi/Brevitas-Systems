-- REVERSE: DDL: re-apply 202607170007_compliance_workflows.sql's compliance_delete_tenant / compliance_delete_subject / compliance_run_retention bodies (renamed per 202607200011:88-101), 202607280016_compliance_warm_state_erasure.sql's compliance_delete_tenant, 202607200011_compliance_billing_isolation.sql's compliance_delete_subject and 202607280023_retention_minimization_and_waitlist.sql's compliance_run_retention verbatim
--
-- Fence erasure and retention on the ledger that actually holds money.
--
-- ============================================================================
-- THE HOLE
-- ============================================================================
--
-- Five statements delete public.usage_log rows. Every one of them is fenced on
-- public.billing_ledger and nothing else:
--
--     and not exists (select 1 from public.billing_ledger ledger
--                      where ledger.usage_log_id = usage.id)
--
--   1. 202607170007:2411-2416  tenant erasure (frozen inner body, renamed to
--                              compliance_delete_tenant_pre_company_identity
--                              by 202607200011:92-93)
--   2. 202607170007:2568-2573  subject erasure, member branch
--   3. 202607170007:2592-2597  subject erasure, customer branch
--   4. 202607280023:257-266    compliance_run_retention's 13-month delete
--   5. 202607280023:166-168    the same predicate as the dry-run candidate count
--
-- billing_ledger has had no writer since 202607280006_retire_per_row_fee_trigger.sql:29
-- dropped queue_brevitas_fee_after_usage, the only INSERT into it
-- (202607200006:225-230); api/server.py:4779 says so in-line. Production
-- confirms it: billing_ledger holds 0 rows and carries no trigger on usage_log.
-- So `not exists (billing_ledger)` is unconditionally TRUE and preserves
-- nothing at all.
--
-- The four preservation invariants that are supposed to catch exactly this are
-- built on the same empty table, so each compares 0 to 0 and can never fire:
--   202607280016:95-97 / :136-140    (live outer compliance_delete_tenant)
--   202607200011:498-500 / :518-523  (live outer compliance_delete_subject)
--   202607170007:2380-2383 / :2450-2454 and :2535-2537 / :2608-2612 (inners)
--
-- Item 4 is the one that runs unattended: scripts/dr/retention.sh:69, nightly,
-- documented at 03:15 UTC by tests/test_dr_compliance_artifacts.py:1312.
--
-- ============================================================================
-- WHAT ACTUALLY HOLDS MONEY
-- ============================================================================
--
-- public.period_settlement_ledger (202607280007:92-201). A settled row is
-- immortal by construction: prevent_period_settlement_delete (202607280007:295-303,
-- trigger :374-376) blocks DELETE outright and organization_id is
-- `on delete restrict`. Its usage_log_watermark_id (202607280007:143) is a
-- PLAIN bigint -- production has 0 foreign-key constraints on that column --
-- and it is the pin the whole re-derivation contract rests on. 202607280031:314-318:
-- "re-deriving a settled basis months later is one call:
-- billing_period_settlement_evidence(org, period_start, period_end, <the
-- settled row's usage_log_watermark_id>) ... The pin itself is immutable ...
-- and deletion is blocked outright."
--
-- That protects the settlement ROW. Nothing protected the usage_log rows the
-- call reads. billing_period_settlement_evidence reads usage_log at three
-- places, all bounded by `id <= watermark` and none bounded below:
--   * the eligible set        202607280031:1078-1089
--   * the observed price set  202607280031:881-889 (ts >= period_start - lookback,
--                             i.e. rows from BEFORE the period)
--   * the anchor set          202607280031:1057-1068 (ancestor.ts >= usage.ts - lookback)
-- Erase a tenant and all three go to zero: the RPC returns eligible_rows = 0
-- and a basis of 0 for a period that was already invoiced. Erase one subject
-- whose rows net negative and the unfloored sum at 202607280031:1097 goes UP,
-- because verified_savings_usd is deliberately signed (20260710_cloud_usage.sql:47,
-- and api/server.py:4764-4772 explains why it must stay signed).
--
-- ============================================================================
-- THE EDIT
-- ============================================================================
--
-- Every one of the five delete predicates gains a second fence:
--
--     and not exists (select 1 from public.period_settlement_ledger settlement
--                      where settlement.organization_id = usage.organization_id
--                        and settlement.status <> 'void'
--                        and settlement.usage_log_watermark_id >= usage.id)
--
-- and each of the four preservation invariants gains a second before/after
-- count over the same predicate, so that a future edit which reopens the fence
-- aborts the transaction instead of silently destroying evidence. The
-- billing_ledger fences and counts are left in place untouched: they cost
-- nothing, and if that table ever acquires a writer again they become live.
--
-- Three deliberate choices in that predicate:
--
--   * `>= usage.id`, not "inside the settled period". The evidence base is NOT
--     the period -- the observed price set and the anchor set both reach back
--     `lookback_days` BEFORE period_start. The watermark is the only bound the
--     settlement itself records, and every re-derivation call is bounded by it,
--     so "at or below the watermark" is exactly the set a re-derivation can
--     read. It over-preserves rows that predate the period; that is the safe
--     direction, and those rows are still content-minimized by the UPDATE that
--     follows each delete, so erasure still removes personal data from them.
--
--   * `status <> 'void'`, which is broader than the live-row definition
--     (`superseded_at is null and status <> 'void'`,
--     period_settlement_ledger_live_period_idx). A superseded revision is what
--     the customer was actually charged on and must stay re-derivable, and a
--     'draft' is the basis that gets promoted to 'pending' -- and per
--     202607280008 a 'pending' settlement is already money. Only 'void', which
--     is a retracted draft, releases its evidence.
--
--   * organization-matched. A settlement pins only its own tenant's rows;
--     usage_log rows with a null organization_id are pinned by nothing, which
--     is correct because billing_period_settlement_evidence's eligible set is
--     `usage.organization_id = p_organization_id` (202607280031:1080).
--
-- NOT CHANGED, deliberately:
--   * The minimizing UPDATEs. Erasure still blanks provider/model/owner_id/etc
--     on every retained row. See the CAVEAT block below.
--   * compliance_run_retention's minimize branch (202607280023:196-198 / :288-289),
--     which is gated on `exists (billing_ledger)` and therefore minimizes
--     nothing today. Re-pointing that gate at period_settlement_ledger would
--     START blanking provider and model on settlement-pinned rows, which is a
--     write, not a preservation, and is out of scope for a fix whose whole
--     direction is "delete strictly less". It is left exactly as it was, and
--     scripts/ci/migration-compliance-evidence-assertions.sql's convergence
--     assertion still governs it.
--
-- CAVEAT this migration does NOT close: the minimizing UPDATE that follows each
-- erasure delete sets provider = '' and model = '' (202607170007:2420, :2586,
-- :2604). billing_observed_model_price joins the price set to the eligible set
-- on (provider, model) (202607280031:1082-1084), so blanking them reclassifies
-- anchored rows as unanchored on re-derivation. The money total
-- sum(eligible.row_savings) is unaffected -- it reads verified_savings_usd --
-- but the anchored/unanchored split the halting guard uses is. Fixing that
-- means changing what erasure minimizes, which is a privacy-posture decision,
-- not a preservation one.
--
-- ============================================================================
-- HOW THE BODIES BELOW WERE PRODUCED
-- ============================================================================
--
-- Mechanically, by line range, never retyped. compliance_delete_tenant_pre_company_identity
-- is 202607170007:2316-2473 with the name changed to the one 202607200011:92-93
-- renamed it to; compliance_delete_subject_pre_company_identity is
-- 202607170007:2475-2630 likewise; compliance_delete_tenant is
-- 202607280016:66-145; compliance_delete_subject is 202607200011:477-528;
-- compliance_run_retention is 202607280023:94-389. Each file's search_path,
-- argument list, argument names, return type, volatility and every other
-- statement are byte-identical to the source; the only differences are the two
-- edits described above. The self-check at the bottom fails closed if any name
-- resolved to more than one pg_proc row, which is what a retyped signature
-- would have produced.
--
-- All five source migrations are SHA-256 pinned by
-- scripts/ci/migration-frozen-checksums.txt and already applied to a production
-- database with no migration ledger, so none of them can be edited in place.
--
-- NOT A DATA CHANGE: no row is inserted, updated or deleted.

begin;

do $migration_precondition$
begin
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception
            '202607280036 requires 202607280007_period_settlement_ledger first'
            using errcode = '55000';
    end if;
    if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'period_settlement_ledger'
           and column_name = 'usage_log_watermark_id'
    ) then
        raise exception
            '202607280036 requires period_settlement_ledger.usage_log_watermark_id'
            using errcode = '55000';
    end if;
    -- Every function below is REPLACED, never created. If one of these is
    -- missing the chain is not where this migration thinks it is, and a
    -- create-or-replace would mint a stray function beside the real one.
    if to_regprocedure('public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)') is null
       or to_regprocedure('public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)') is null
       or to_regprocedure('public.compliance_delete_tenant(uuid,uuid,text)') is null
       or to_regprocedure('public.compliance_delete_subject(uuid,uuid,text)') is null
       or to_regprocedure('public.compliance_run_retention(uuid,text,integer,boolean)') is null
    then
        raise exception
            '202607280036 requires the company-scoped compliance erasure and retention chain'
            using errcode = '55000';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- 1/5. Tenant erasure, frozen inner body (202607170007:2316-2473).
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_tenant_pre_company_identity(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_key_hashes text[];
    v_member_ids uuid[];
    v_member_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform 1 from public.organizations where id = p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode = 'P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id = p_request_id and organization_id = p_organization_id for update;
    if not found or v_request.request_type <> 'delete'
       or v_request.request_scope <> 'tenant' then
        raise exception 'approved tenant deletion request not found' using errcode = 'P0002';
    end if;
    if v_request.status = 'completed' then
        if not exists (select 1 from public.backup_deletion_tombstones
                        where request_id = p_request_id and organization_id = p_organization_id) then
            raise exception 'completed deletion is missing its tombstone' using errcode = '55000';
        end if;
        return 'completed';
    end if;
    if v_request.status not in ('approved', 'processing') then
        raise exception 'tenant deletion request is not approved' using errcode = '55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id, 'delete') then
        raise exception 'tenant deletion is blocked by legal hold' using errcode = '55000';
    end if;
    perform public.compliance_erase_support_records(p_organization_id,'tenant',null);
    v_was_approved := v_request.status = 'approved';
    update public.data_subject_requests
       set status = 'processing', started_at = coalesce(started_at, clock_timestamp())
     where id = p_request_id and organization_id = p_organization_id;
    perform public.compliance_record_deadline_breach(v_request, p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
            p_request_id::text, 'compliance.delete.started',
            'data_subject_request', p_request_id::text, 'committed'
        );
    end if;

    select coalesce(array_agg(key_hash), array[]::text[]) into v_key_hashes
      from public.api_keys where organization_id = p_organization_id;
    select coalesce(array_agg(distinct identity_id), array[]::uuid[]) into v_member_ids
      from (
        select user_id as identity_id from public.organization_members
         where organization_id=p_organization_id
        union all
        select billing_owner_id from public.organizations
         where id=p_organization_id and billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.organization_id = p_organization_id;
    -- 202607280036: the settlement evidence base. billing_ledger has had no
    -- writer since 202607280006, so the count above is 0 = 0 and preserves
    -- nothing. period_settlement_ledger is what actually holds money.
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    -- Revoke access before removing configuration. All statements share this
    -- function transaction; any failure rolls back the entire deletion.
    update public.api_keys set revoked_at = coalesce(revoked_at, clock_timestamp())
     where organization_id = p_organization_id;
    update public.service_accounts
       set status = 'revoked', revoked_at = coalesce(revoked_at, clock_timestamp()),
           updated_at = clock_timestamp()
     where organization_id = p_organization_id;

    delete from public.bvx_device_auth
     where key_hash = any(v_key_hashes)
        or owner_id in (select member_id::text from unnest(v_member_ids) member_id);
    delete from public.provider_config where key_hash = any(v_key_hashes);
    delete from public.ai_jobs where organization_id = p_organization_id;
    delete from public.semantic_cache cache
     where cache.tenant_namespace = encode(digest(p_organization_id::text, 'sha256'), 'hex')
        or cache.tenant_namespace = encode(
            digest(p_organization_id::text || ':unattributed', 'sha256'), 'hex'
        )
        or cache.tenant_namespace in (
            select encode(digest(p_organization_id::text || ':' || customer.id::text, 'sha256'), 'hex')
              from public.customers customer
             where customer.organization_id = p_organization_id
        );

    -- Keep only usage rows required by the immutable financial ledger. They are
    -- minimized to content-free financial evidence; billing/tax retention is
    -- seven years subject to counsel. Non-ledger usage follows the shorter policy.
    delete from public.usage_log usage
     where usage.organization_id = p_organization_id
       and not exists (select 1 from public.billing_ledger ledger
                        where ledger.usage_log_id = usage.id)
       and not exists (select 1 from public.period_settlement_ledger settlement
                        where settlement.organization_id = usage.organization_id
                          and settlement.status <> 'void'
                          and settlement.usage_log_watermark_id >= usage.id);
    update public.usage_log usage
       set customer_id = null,
           key_hash = 'deleted-' || p_organization_id::text,
           owner_id = '', project = 'Deleted', environment = 'Deleted', source = 'Deleted',
           repo = '', client = '', agent = '', call_site_id = '', framework = '', gateway = '',
           provider = '', model = '', session_id = '', pipeline = '', run_id = '', request_id = '',
           usage_raw = ''
     where usage.organization_id = p_organization_id;

    if to_regclass('public.key_repositories') is not null then
        execute 'delete from public.key_repositories where key_hash = any($1)'
          using v_key_hashes;
    end if;
    if to_regclass('public.bvx_device_consumption_receipts') is not null then
        execute 'delete from public.bvx_device_consumption_receipts where organization_id=$1'
          using p_organization_id;
    end if;
    delete from public.api_keys where organization_id = p_organization_id;
    delete from public.installations where organization_id = p_organization_id;
    delete from public.devices where organization_id = p_organization_id;
    delete from public.service_accounts where organization_id = p_organization_id;
    delete from public.organization_invitations where organization_id = p_organization_id;
    delete from public.organization_members where organization_id = p_organization_id;
    foreach v_member_id in array v_member_ids loop
        perform public.compliance_anonymize_unshared_user(v_member_id);
    end loop;
    delete from public.customers where organization_id = p_organization_id;
    update public.organizations
       set name = 'Deleted organization', legacy_owner_id = null,
           billing_owner_id = null, cache_enabled = false
     where id = p_organization_id;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'financial preservation invariant failed' using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;

    insert into public.backup_deletion_tombstones(
        request_id, organization_id, request_received_at, expires_at
    ) values (
        p_request_id, p_organization_id, v_request.requested_at,
        v_request.requested_at + interval '35 days'
    ) on conflict (request_id) do nothing;
    update public.data_subject_requests
       set status = 'completed', completed_at = clock_timestamp()
     where id = p_request_id and organization_id = p_organization_id;
    perform public.append_company_audit(
        p_organization_id, p_actor_id, public.compliance_actor_role(p_actor_id),
        p_request_id::text, 'compliance.delete.completed',
        'data_subject_request', p_request_id::text, 'committed'
    );
    return 'completed';
end;
$$;

-- ---------------------------------------------------------------------------
-- 2/5. Subject erasure, frozen inner body (202607170007:2475-2630). BOTH the
--      member branch and the customer branch carry the dead fence.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_subject_pre_company_identity(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_request public.data_subject_requests%rowtype;
    v_key_hashes text[] := array[]::text[];
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
    v_was_approved boolean;
begin
    perform public.compliance_actor_role(p_actor_id);
    perform 1 from public.organizations where id=p_organization_id for update;
    if not found then raise exception 'organization not found' using errcode='P0002'; end if;
    select * into v_request from public.data_subject_requests
     where id=p_request_id and organization_id=p_organization_id for update;
    if not found or v_request.request_type<>'delete'
       or v_request.request_scope not in ('member','customer') then
        raise exception 'approved subject deletion request not found' using errcode='P0002';
    end if;
    if v_request.status='completed' then
        if not exists (select 1 from public.backup_deletion_tombstones
                        where request_id=p_request_id and organization_id=p_organization_id) then
            raise exception 'completed subject deletion is missing its tombstone' using errcode='55000';
        end if;
        return 'completed';
    end if;
    if v_request.status not in ('approved','processing') then
        raise exception 'subject deletion request is not approved' using errcode='55000';
    end if;
    if public.compliance_request_has_hold(p_organization_id,'delete') then
        raise exception 'subject deletion is blocked by legal hold' using errcode='55000';
    end if;
    perform public.compliance_erase_support_records(
        p_organization_id,v_request.request_scope,v_request.subject_id);
    if v_request.request_scope='member' then
        perform 1 from public.organization_members
         where organization_id=p_organization_id and user_id=v_request.subject_id for update;
    else
        perform 1 from public.customers
         where organization_id=p_organization_id and id=v_request.subject_id for update;
    end if;
    if not found then raise exception 'subject not found in organization' using errcode='P0002'; end if;
    v_was_approved := v_request.status='approved';
    update public.data_subject_requests
       set status='processing',started_at=coalesce(started_at,clock_timestamp())
     where id=p_request_id and organization_id=p_organization_id;
    perform public.compliance_record_deadline_breach(v_request,p_actor_id);
    if v_was_approved then
        perform public.append_company_audit(
            p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
            p_request_id::text,'compliance.subject_delete.started',
            v_request.request_scope,v_request.subject_id::text,'committed'
        );
    end if;
    select count(*) into v_billing_before from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id=p_organization_id;
    -- 202607280036: see the note in compliance_delete_tenant_pre_company_identity.
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id=p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id=usage.organization_id
                      and settlement.status<>'void'
                      and settlement.usage_log_watermark_id>=usage.id);

    if v_request.request_scope='member' then
        select coalesce(array_agg(key_hash),array[]::text[]) into v_key_hashes
          from public.api_keys
         where organization_id=p_organization_id
           and owner_id=v_request.subject_id::text
           and key_type in ('dashboard_session','device');
        delete from public.ai_jobs where key_hash=any(v_key_hashes);
        delete from public.bvx_device_auth
         where key_hash=any(v_key_hashes) or owner_id=v_request.subject_id::text;
        delete from public.provider_config where key_hash=any(v_key_hashes);
        if to_regclass('public.key_repositories') is not null then
            execute 'delete from public.key_repositories where key_hash=any($1)'
              using v_key_hashes;
        end if;
        if to_regclass('public.bvx_device_consumption_receipts') is not null then
            execute 'delete from public.bvx_device_consumption_receipts where organization_id=$1 and owner_id=$2'
              using p_organization_id,v_request.subject_id;
        end if;
        delete from public.api_keys where key_hash=any(v_key_hashes);
        update public.api_keys set owner_id='',created_by=null
         where organization_id=p_organization_id
           and owner_id=v_request.subject_id::text;
        update public.service_accounts set created_by=null,updated_at=clock_timestamp()
         where organization_id=p_organization_id and created_by=v_request.subject_id;
        update public.organization_invitations set accepted_by=null
         where organization_id=p_organization_id and accepted_by=v_request.subject_id
           and invited_by<>v_request.subject_id;
        delete from public.organization_invitations
         where organization_id=p_organization_id and invited_by=v_request.subject_id;
        delete from public.usage_log usage
         where usage.organization_id=p_organization_id
           and usage.owner_id=v_request.subject_id::text
           and not exists (select 1 from public.billing_ledger ledger
                            where ledger.usage_log_id=usage.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=usage.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=usage.id);
        update public.usage_log usage
           set owner_id='',customer_id=null,key_hash='deleted-'||p_organization_id::text,
               project='Deleted',environment='Deleted',source='Deleted',repo='',client='',agent='',
               call_site_id='',framework='',gateway='',provider='',model='',session_id='',
               pipeline='',run_id='',request_id='',usage_raw=''
         where usage.organization_id=p_organization_id
           and usage.owner_id=v_request.subject_id::text;
        delete from public.organization_members
         where organization_id=p_organization_id and user_id=v_request.subject_id;
        update public.organizations set billing_owner_id=null
         where id=p_organization_id and billing_owner_id=v_request.subject_id;
        perform public.compliance_anonymize_unshared_user(v_request.subject_id);
    else
        delete from public.ai_jobs
         where organization_id=p_organization_id and customer_id=v_request.subject_id;
        delete from public.semantic_cache
         where tenant_namespace=encode(
             digest(p_organization_id::text||':'||v_request.subject_id::text,'sha256'),'hex'
         );
        delete from public.usage_log usage
         where usage.organization_id=p_organization_id
           and usage.customer_id=v_request.subject_id
           and not exists (select 1 from public.billing_ledger ledger
                            where ledger.usage_log_id=usage.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=usage.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=usage.id);
        update public.usage_log usage
           set customer_id=null,key_hash='deleted-'||p_organization_id::text,
               owner_id='',project='Deleted',environment='Deleted',source='Deleted',
               repo='',client='',agent='',call_site_id='',framework='',gateway='',
               provider='',model='',session_id='',pipeline='',run_id='',request_id='',usage_raw=''
         where usage.organization_id=p_organization_id
           and usage.customer_id=v_request.subject_id;
        delete from public.customers
         where organization_id=p_organization_id and id=v_request.subject_id;
    end if;

    select count(*) into v_billing_after from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.organization_id=p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject financial preservation invariant failed' using errcode='55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id=p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id=usage.organization_id
                      and settlement.status<>'void'
                      and settlement.usage_log_watermark_id>=usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'subject settlement evidence preservation invariant failed'
            using errcode='55000';
    end if;
    insert into public.backup_deletion_tombstones(
        request_id,organization_id,request_received_at,expires_at
    ) values (
        p_request_id,p_organization_id,v_request.requested_at,
        v_request.requested_at+interval '35 days'
    ) on conflict (request_id) do nothing;
    update public.data_subject_requests
       set status='completed',completed_at=clock_timestamp()
     where id=p_request_id and organization_id=p_organization_id;
    perform public.append_company_audit(
        p_organization_id,p_actor_id,public.compliance_actor_role(p_actor_id),
        p_request_id::text,'compliance.subject_delete.completed',
        v_request.request_scope,v_request.subject_id::text,'committed'
    );
    return 'completed';
end;
$$;

-- ---------------------------------------------------------------------------
-- 3/5. Tenant erasure, live outer wrapper (202607280016:66-145). Only the
--      invariant changes here; the deletes it wraps are in 1/5.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_identity_ids uuid[];
    v_identity_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select coalesce(
        pg_catalog.array_agg(distinct identity_id), array[]::uuid[]
    ) into v_identity_ids
      from (
        select member.user_id as identity_id
          from public.organization_members member
         where member.organization_id = p_organization_id
        union all
        select organization.billing_owner_id
          from public.organizations organization
         where organization.id = p_organization_id
           and organization.billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    v_result := public.compliance_delete_tenant_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    -- Warming state was introduced by 202607280001, after the frozen inner
    -- body was written, so nothing in the chain deletes it. warm_credentials
    -- holds a KMS-encrypted third-party provider key plus the named consent
    -- actor and consent timestamp; it cascades only from public.organizations,
    -- and tenant deletion deliberately RENAMES that row instead of deleting it,
    -- so the cascade never fires. warm_prefixes cascades from public.customers
    -- (deleted by the inner body) and is repeated here as belt and braces.
    -- Both deletes are inside the caller's transaction, so the whole deletion
    -- still rolls back as one unit, and they re-run for an already-'completed'
    -- request because the inner body short-circuits before them.
    delete from public.warm_credentials credential
     where credential.organization_id = p_organization_id;
    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id;
    -- public.warm_budget_ledger is intentionally NOT deleted. It is content-free
    -- (organization, provider, day, reserved/spent money) and it is the operand
    -- billing_period_settlement_evidence recomputes the warm deduction from
    -- (202607280008:344-350), so erasing it would silently raise the fee ceiling
    -- for a retained period. It ages out on its own retention horizon in
    -- public.purge_warm_state.

    update public.billing_accounts account
       set checkout_session_id = null,
           updated_at = pg_catalog.clock_timestamp()
     where account.organization_id = p_organization_id;
    update public.billing_events event
       set session_id = ''
     where event.organization_id = p_organization_id;

    foreach v_identity_id in array v_identity_ids loop
        perform public.compliance_anonymize_unshared_user(v_identity_id);
    end loop;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4/5. Subject erasure, live outer wrapper (202607200011:477-528). Invariant
--      only, as in 3/5.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_request_scope text;
    v_subject_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select request.request_scope, request.subject_id
      into v_request_scope, v_subject_id
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    v_result := public.compliance_delete_subject_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    if v_request_scope = 'member' then
        update public.billing_accounts account
           set checkout_session_id = null,
               updated_at = pg_catalog.clock_timestamp()
         where account.organization_id = p_organization_id
           and account.user_id = v_subject_id;
        update public.billing_events event
           set session_id = ''
         where event.organization_id = p_organization_id
           and event.user_id = v_subject_id;
        perform public.compliance_anonymize_unshared_user(v_subject_id);
    end if;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'subject company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 5/5. compliance_run_retention (202607280023:94-389). The only unattended
--      deleter of usage_log: scripts/dr/retention.sh:69, nightly. Both the
--      dry-run candidate count and the apply-time delete are fenced, so the
--      count the immutable compliance_retention_runs evidence row records still
--      matches what the delete would actually remove.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_run_retention(
    p_run_id uuid,
    p_actor_id text,
    p_batch_limit integer,
    p_apply boolean
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_usage_cutoff timestamptz := clock_timestamp()-interval '13 months';
    v_support_cutoff timestamptz := clock_timestamp()-interval '24 months';
    -- Prospect contact data has never had a retention rule. 24 months from
    -- submission, matching the support-record period.
    v_waitlist_cutoff timestamptz := clock_timestamp()-interval '24 months';
    v_evidence_cutoff timestamptz := clock_timestamp()-interval '400 days';
    v_existing public.compliance_retention_runs%rowtype;
    v_usage_candidates integer := 0;
    v_audit_candidates integer := 0;
    v_support_candidates integer := 0;
    v_request_candidates integer := 0;
    v_hold_candidates integer := 0;
    v_prior_run_candidates integer := 0;
    v_usage_minimize_candidates integer := 0;
    v_waitlist_candidates integer := 0;
    v_usage_deleted integer := 0;
    v_audit_deleted integer := 0;
    v_support_deleted integer := 0;
    v_requests_deleted integer := 0;
    v_holds_deleted integer := 0;
    v_prior_run_deleted integer := 0;
    v_usage_minimized integer := 0;
    v_waitlist_deleted integer := 0;
    v_hold_ids uuid[] := array[]::uuid[];
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_run_id is null or p_apply is null or p_batch_limit is null
       or p_batch_limit not between 1 and 10000 then
        raise exception 'retention batch limit must be between 1 and 10000' using errcode='22023';
    end if;
    select * into v_existing from public.compliance_retention_runs where id=p_run_id;
    if found then
        if not p_apply or v_existing.actor_id<>p_actor_id
           or v_existing.batch_limit<>p_batch_limit then
            raise exception 'retention run idempotency conflict' using errcode='23505';
        end if;
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','apply',
            'run_id',v_existing.id,'batch_limit',v_existing.batch_limit,
            'usage_candidates',v_existing.usage_candidates,
            'audit_candidates',v_existing.audit_candidates,
            'support_candidates',v_existing.support_candidates,
            'requests_candidates',v_existing.requests_candidates,
            'holds_candidates',v_existing.holds_candidates,
            'prior_run_evidence_candidates',v_existing.prior_run_evidence_candidates,
            'usage_deleted',v_existing.usage_deleted,
            'audit_deleted',v_existing.audit_deleted,
            'support_deleted',v_existing.support_deleted,
            'requests_deleted',v_existing.requests_deleted,
            'holds_deleted',v_existing.holds_deleted,
            'prior_run_evidence_deleted',v_existing.prior_run_evidence_deleted,
            'usage_minimize_candidates',v_existing.usage_minimize_candidates,
            'usage_minimized',v_existing.usage_minimized,
            'waitlist_candidates',v_existing.waitlist_candidates,
            'waitlist_deleted',v_existing.waitlist_deleted,
            'idempotent_replay',true,'evidence_contains_customer_content',false
        );
    end if;

    select count(*)::integer into v_usage_candidates from (
        select 1 from public.usage_log usage
         where usage.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=usage.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=usage.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=usage.id)
           and not public.compliance_preservation_hold(usage.organization_id)
         order by usage.ts,usage.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_audit_candidates from (
        select 1 from public.audit_events event
         where event.occurred_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(event.organization_id)
         order by event.occurred_at,event.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_request_candidates from (
        select 1 from public.data_subject_requests request
         where request.status='completed' and request.completed_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(request.organization_id)
         order by request.completed_at,request.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_hold_candidates from (
        select 1 from public.legal_holds hold
         where not hold.active and hold.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(hold.organization_id)
         order by hold.released_at,hold.id limit p_batch_limit
    ) candidate;
    -- Ledger-referenced usage rows cannot be deleted, and nothing ever
    -- minimized them: compliance_run_retention is delete-only. The deletion path
    -- already classifies these exact columns as needing removal
    -- (202607170007:2417-2423), so past the same 13-month cutoff the rows the
    -- ledger forces us to keep get the same treatment.
    select count(*)::integer into v_usage_minimize_candidates from (
        select 1 from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted')
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_waitlist_candidates from (
        select 1 from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_prior_run_candidates from (
        select 1 from public.compliance_retention_runs run
         where run.completed_at<v_evidence_cutoff
           and not public.compliance_global_preservation_hold()
         order by run.completed_at,run.id limit p_batch_limit
    ) candidate;

    if to_regclass('public.support_records') is not null then
        if not exists (select 1 from information_schema.columns
                        where table_schema='public' and table_name='support_records'
                          and column_name='organization_id')
           or not exists (select 1 from information_schema.columns
                           where table_schema='public' and table_name='support_records'
                             and column_name='created_at') then
            raise exception 'support_records retention contract is unsupported' using errcode='55000';
        end if;
        execute 'select count(*)::integer from (select 1 from public.support_records support where support.created_at<$1 and not public.compliance_preservation_hold(support.organization_id) order by support.created_at,support.ctid limit $2) candidate'
          into v_support_candidates using v_support_cutoff,p_batch_limit;
    end if;

    if not p_apply then
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','dry_run',
            'run_id',p_run_id,'batch_limit',p_batch_limit,
            'usage_candidates',v_usage_candidates,
            'audit_candidates',v_audit_candidates,
            'support_candidates',v_support_candidates,
            'requests_candidates',v_request_candidates,
            'holds_candidates',v_hold_candidates,
            'prior_run_evidence_candidates',v_prior_run_candidates,
            'usage_minimize_candidates',v_usage_minimize_candidates,
            'waitlist_candidates',v_waitlist_candidates,
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
            'usage_minimized',0,'waitlist_deleted',0,
            'idempotent_replay',false,'evidence_contains_customer_content',false
        );
    end if;

    delete from public.usage_log usage
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=candidate.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_deleted = row_count;

    -- Minimize what the ledger forces us to retain. The preserved columns are
    -- exactly the ones the financial evidence needs -- id, organization_id, ts,
    -- authoritative, pricing_status and the token/price/savings columns, verified
    -- against billing_period_settlement_evidence (202607280008:325-363) -- plus
    -- key_hash and request_id, which are NOT cleared here even though the
    -- deletion path clears them: usage_log carries a unique index on
    -- (key_hash, request_id) where request_id<>'', so rewriting key_hash while
    -- leaving request_id in place could collide two retained rows and abort the
    -- retention run, and the seven-year financial evidence is correlated by
    -- request_id in the DR assertions. Clearing both (as tenant erasure does) is
    -- correct only when the whole tenant is going away.
    update public.usage_log usage
       set customer_id = null,
           owner_id = '', project = 'Deleted', environment = 'Deleted',
           source = 'Deleted', repo = '', client = '', agent = '',
           call_site_id = '', framework = '', gateway = '', provider = '',
           model = '', session_id = '', pipeline = '', run_id = '',
           usage_raw = ''
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted')
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_minimized = row_count;

    -- Waitlist prospects never created an account, so no data_subject_requests
    -- scope can reach them: every implemented scope requires an organization_id
    -- or a subject inside one. This is the only retention path they have.
    delete from public.waitlist entry
     where entry.id in (
        select candidate.id from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_waitlist_deleted = row_count;

    if to_regclass('public.support_records') is not null then
        execute 'delete from public.support_records support where support.ctid in (select candidate.ctid from public.support_records candidate where candidate.created_at<$1 and not public.compliance_preservation_hold(candidate.organization_id) order by candidate.created_at,candidate.ctid for update skip locked limit $2)'
          using v_support_cutoff,p_batch_limit;
        get diagnostics v_support_deleted = row_count;
    end if;

    select deleted.audit_deleted,deleted.requests_deleted,deleted.prior_run_evidence_deleted
      into v_audit_deleted,v_requests_deleted,v_prior_run_deleted
      from public.compliance_retention_delete_immutable(v_evidence_cutoff,p_batch_limit) deleted;
    select coalesce(array_agg(candidate.id order by candidate.released_at,candidate.id),
                    array[]::uuid[])
      into v_hold_ids
      from (
        select candidate.id,candidate.released_at from public.legal_holds candidate
         where not candidate.active and candidate.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.released_at,candidate.id
         for update skip locked
         limit p_batch_limit
     ) candidate;
    begin
        execute 'alter table public.legal_hold_actions disable trigger legal_hold_actions_enforce_transition';
        delete from public.legal_hold_actions hold_action
         where hold_action.target_hold_id=any(v_hold_ids);
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
    exception when others then
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
        raise;
    end;
    delete from public.legal_holds hold where hold.id=any(v_hold_ids);
    get diagnostics v_holds_deleted = row_count;

    insert into public.compliance_retention_runs(
        id,actor_id,batch_limit,usage_candidates,audit_candidates,support_candidates,
        requests_candidates,holds_candidates,prior_run_evidence_candidates,
        usage_deleted,audit_deleted,support_deleted,
        requests_deleted,holds_deleted,prior_run_evidence_deleted,
        usage_minimize_candidates,waitlist_candidates,
        usage_minimized,waitlist_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted,
        v_usage_minimize_candidates,v_waitlist_candidates,
        v_usage_minimized,v_waitlist_deleted
    );
    perform public.append_company_audit(
        null,p_actor_id,public.compliance_actor_role(p_actor_id),p_run_id::text,
        'compliance.retention.completed','retention_run',p_run_id::text,'committed'
    );
    return jsonb_build_object(
        'schema','brevitas.compliance-retention-result.v1','mode','apply',
        'run_id',p_run_id,'batch_limit',p_batch_limit,
        'usage_candidates',v_usage_candidates,'audit_candidates',v_audit_candidates,
        'support_candidates',v_support_candidates,'requests_candidates',v_request_candidates,
        'holds_candidates',v_hold_candidates,
        'prior_run_evidence_candidates',v_prior_run_candidates,
        'usage_deleted',v_usage_deleted,'audit_deleted',v_audit_deleted,
        'support_deleted',v_support_deleted,'requests_deleted',v_requests_deleted,
        'holds_deleted',v_holds_deleted,'prior_run_evidence_deleted',v_prior_run_deleted,
        'usage_minimize_candidates',v_usage_minimize_candidates,
        'usage_minimized',v_usage_minimized,
        'waitlist_candidates',v_waitlist_candidates,
        'waitlist_deleted',v_waitlist_deleted,
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

-- Grants are unchanged by create-or-replace, but 202607280032's contract says
-- every migration that defines a routine restates its own posture rather than
-- inheriting one. These reproduce 202607200011:110-115, 202607200011:527-543
-- and 202607170007:2773-2802 exactly.
revoke all on function public.compliance_delete_tenant_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_subject_pre_company_identity(
    uuid,uuid,text
) from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_tenant(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_delete_subject(uuid,uuid,text)
    from public, anon, authenticated, service_role;
revoke all on function public.compliance_run_retention(uuid,text,integer,boolean)
    from public, anon, authenticated;
grant execute on function public.compliance_delete_tenant(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_delete_subject(uuid,uuid,text)
    to service_role;
grant execute on function public.compliance_run_retention(uuid,text,integer,boolean)
    to service_role;

comment on function public.compliance_delete_tenant(uuid,uuid,text) is
    'Tenant erasure. Preserves usage_log rows at or below a non-void period_settlement_ledger watermark for the organization, and aborts if that set changes across the deletion.';
comment on function public.compliance_delete_subject(uuid,uuid,text) is
    'Subject erasure. Preserves usage_log rows at or below a non-void period_settlement_ledger watermark for the organization, and aborts if that set changes across the deletion.';

-- Apply-time self-check. Everything below reads the catalog. It proves the
-- fence landed on all five bodies, the invariant on all four, that each name
-- still resolves to exactly ONE pg_proc row (a mistyped signature would have
-- created an overload beside the original and left the hole open on whichever
-- one the caller resolved to), and that the browser roles still cannot reach
-- any of them.
do $contract$
declare
    v_name text;
    v_src text;
    v_count integer;
    v_expected integer;
    v_fence constant text :=
        'not exists (select 1 from public.period_settlement_ledger settlement';
begin
    foreach v_name in array array[
        'compliance_delete_tenant_pre_company_identity',
        'compliance_delete_subject_pre_company_identity',
        'compliance_delete_tenant',
        'compliance_delete_subject',
        'compliance_run_retention'
    ] loop
        select count(*) into v_count
          from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public' and procedure.proname = v_name;
        if v_count <> 1 then
            raise exception
                '202607280036 left % public.% functions -- an overload, not a replacement',
                v_count, v_name using errcode = '55000';
        end if;
        select procedure.prosrc into v_src
          from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public' and procedure.proname = v_name;
        if v_src not like '%period_settlement_ledger%' then
            raise exception
                'public.% still fences usage_log deletion on billing_ledger alone', v_name
                using errcode = '55000';
        end if;
        if not exists (
            select 1 from pg_proc procedure
              join pg_namespace namespace on namespace.oid = procedure.pronamespace
             where namespace.nspname = 'public' and procedure.proname = v_name
               and procedure.prosecdef
        ) then
            raise exception 'public.% lost SECURITY DEFINER', v_name
                using errcode = '55000';
        end if;
    end loop;

    -- The delete fence is per-statement, so count them: 1 in tenant erasure,
    -- 2 in subject erasure (member + customer), 2 in retention (candidate
    -- count + delete).
    for v_name, v_expected in
        select fence.name, fence.expected
          from unnest(
                array[
                    'compliance_delete_tenant_pre_company_identity',
                    'compliance_delete_subject_pre_company_identity',
                    'compliance_run_retention'
                ],
                array[1, 2, 2]
          ) as fence(name, expected)
    loop
        select (length(procedure.prosrc)
                - length(replace(procedure.prosrc, v_fence, '')))
               / length(v_fence)
          into v_count
          from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public' and procedure.proname = v_name;
        if v_count <> v_expected then
            raise exception
                'public.% carries % settlement delete fences, expected %',
                v_name, v_count, v_expected
                using errcode = '55000';
        end if;
    end loop;

    -- The invariant, on all four erasure entry points.
    foreach v_name in array array[
        'compliance_delete_tenant_pre_company_identity',
        'compliance_delete_subject_pre_company_identity',
        'compliance_delete_tenant',
        'compliance_delete_subject'
    ] loop
        select procedure.prosrc into v_src
          from pg_proc procedure
          join pg_namespace namespace on namespace.oid = procedure.pronamespace
         where namespace.nspname = 'public' and procedure.proname = v_name;
        if v_src not like '%v_settlement_before%'
           or v_src not like '%v_settlement_after%'
           or v_src not like '%settlement evidence preservation invariant failed%' then
            raise exception
                'public.% has no settlement evidence preservation invariant', v_name
                using errcode = '55000';
        end if;
    end loop;

    if has_function_privilege('anon', 'public.compliance_delete_tenant(uuid,uuid,text)', 'execute')
       or has_function_privilege('anon', 'public.compliance_delete_subject(uuid,uuid,text)', 'execute')
       or has_function_privilege('anon', 'public.compliance_run_retention(uuid,text,integer,boolean)', 'execute')
       or has_function_privilege('authenticated', 'public.compliance_delete_tenant(uuid,uuid,text)', 'execute')
       or has_function_privilege('authenticated', 'public.compliance_delete_subject(uuid,uuid,text)', 'execute')
       or has_function_privilege('authenticated', 'public.compliance_run_retention(uuid,text,integer,boolean)', 'execute')
       or has_function_privilege('anon', 'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)', 'execute')
       or has_function_privilege('authenticated', 'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)', 'execute')
    then
        raise exception 'the compliance erasure path is browser-executable'
            using errcode = '42501';
    end if;
    if has_function_privilege('service_role',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)', 'execute')
       or has_function_privilege('service_role',
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)', 'execute')
    then
        raise exception 'the private erasure inners became service-role callable'
            using errcode = '42501';
    end if;
    if not has_function_privilege('service_role', 'public.compliance_delete_tenant(uuid,uuid,text)', 'execute')
       or not has_function_privilege('service_role', 'public.compliance_delete_subject(uuid,uuid,text)', 'execute')
       or not has_function_privilege('service_role', 'public.compliance_run_retention(uuid,text,integer,boolean)', 'execute')
    then
        raise exception 'the API service role lost the compliance erasure path'
            using errcode = '42501';
    end if;
end
$contract$;

commit;
