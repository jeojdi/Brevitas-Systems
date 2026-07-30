-- Record successful audit reads, and record what a permission change changed.
--
-- TWO ASYMMETRIES. First, company_admin_audit_page appends an audit row only
-- when authorization FAILS and then returns the page silently on success
-- (202607170005_company_administration.sql:869-885). The same asymmetry runs
-- across the whole company-admin read surface -- SupabaseCompanyAdminService._require
-- (api/company_admin.py:1019-1039) appends only in the denial branch -- so
-- company.read, members:read, service_accounts:read and audit:read successes
-- leave no trace at all. Who READ the tenant's audit log is exactly the fact an
-- audit log is supposed to answer.
--
-- The naive fix is self-defeating: appending 'audit.read' on every successful
-- page makes the log self-polluting, because each read inserts a row that
-- appears in the next page, and audit_events is append-only for every role
-- including service_role (reject_audit_event_mutation, :158-167) with a 400-day
-- retention obligation (docs/COMPANY_ADMINISTRATION.md:195). So both halves ship
-- together: at most ONE read event per request, deduplicated on
-- (organization_id, request_id, action) by a partial unique index -- p_request_id
-- is already threaded through the RPC and audit_events.request_id is NOT NULL --
-- and '%.read' actions are excluded from the rows the page returns, so paging
-- cannot feed on itself. Denials are unaffected: 'audit.read.denied' does not
-- match '%.read'.
--
-- Second, audit_events structurally cannot carry before/after values:
-- validate_audit_event_insert rejects any row whose `details <> '{}'::jsonb`
-- (:176-183) and append_company_audit always inserts '{}' (:244). So
-- company_admin_set_member mutates role AND status and records only the target
-- user id (:529-535), and service_account.created omits the granted scopes
-- (:588). That trigger is the enforcement point for the content-free policy and
-- is NOT relaxed here -- relaxing it would invite payload creep into an immutable
-- table. Instead the low-cardinality transitions are encoded in the fields that
-- already exist and already have tight regexes: `action` (4 roles, 3 statuses ->
-- 'member.role_changed.member_to_company_owner') and `target_id` (the fixed
-- 13-value scope allowlist -> '<service_account_id>:proxy:invoke'). The existing
-- 'member.changed' and 'service_account.created' events still fire, so nothing
-- pinned to them changes meaning.
--
-- `actor_ip inet` / `actor_session_id uuid` are deliberately NOT added: they
-- would put PII into the immutable 400-day table and would need the erasure paths
-- at 202607170007:529-594 extended plus a privacy-notice review. That is a
-- separate proposal, not a rider on this one.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- dropping the read/transition evidence surface deletes audit evidence; forward-only by the same rule as the audit tables

begin;

do $migration_precondition$
begin
    if to_regprocedure(
        'public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)'
    ) is null
       or to_regprocedure(
        'public.company_admin_set_member(uuid,uuid,uuid,text,text,text)'
    ) is null
       or to_regprocedure(
        'public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text)'
    ) is null
       or to_regprocedure(
        'public.append_company_audit(uuid,text,text,text,text,text,text,text)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202607280022 requires the company administration RPC boundary';
    end if;
end;
$migration_precondition$;

-- One read event per (organization, request). Partial so it constrains only the
-- read actions this migration introduces and never the mutation/denial history.
-- No '%.read' rows exist yet -- that is the defect -- so the index cannot conflict
-- with retained evidence.
create unique index if not exists audit_events_read_request_idx
    on public.audit_events(organization_id, request_id, action)
    where action like '%.read';

-- Read evidence is idempotent by construction; every other append stays strict.
create or replace function public.append_company_audit_read(
    p_organization_id uuid,
    p_actor_id text,
    p_actor_role text,
    p_request_id text,
    p_action text,
    p_target_type text,
    p_target_id text
) returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_id bigint;
begin
    if p_organization_id is null
       or p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_action !~ '^[a-z0-9_.-]{3,100}$'
       or p_action not like '%.read'
       or p_target_type !~ '^[a-z0-9_.-]{1,50}$'
       or char_length(p_target_id) not between 1 and 200 then
        raise exception 'invalid content-free audit read fields' using errcode = '22023';
    end if;
    insert into public.audit_events(
        organization_id, actor_user_id, actor_id, actor_role, request_id,
        action, target_type, target_id, outcome, details
    ) values (
        p_organization_id,
        case when p_actor_id ~ '^[0-9a-fA-F-]{36}$' then p_actor_id::uuid else null end,
        p_actor_id, p_actor_role, p_request_id,
        p_action, p_target_type, p_target_id, 'committed', '{}'::jsonb
    )
    on conflict (organization_id, request_id, action)
        where action like '%.read'
        do nothing
    returning id into v_id;
    -- Null means "already recorded for this request", which is a success.
    return v_id;
end;
$$;
revoke all on function public.append_company_audit_read(
    uuid,text,text,text,text,text,text
) from public, anon, authenticated;
grant execute on function public.append_company_audit_read(
    uuid,text,text,text,text,text,text
) to service_role;

create or replace function public.company_admin_audit_page(
    p_organization_id uuid, p_actor_user_id uuid,
    p_cursor_time timestamptz, p_cursor_id bigint, p_limit integer,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_role text; v_items jsonb;
begin
    v_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_role not in ('company_owner','company_admin','billing_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_role,'none'),p_request_id,'audit.read.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    -- Record the successful read, not only the denial. Deduplicated on
    -- (organization, request_id, action) so paging a hundred rows appends one
    -- event, and '%.read' actions are excluded from the page below so the log
    -- cannot feed on itself: without both halves a scripted walk would inflate
    -- an append-only, 400-day-retention table with evidence of itself.
    perform public.append_company_audit_read(
        p_organization_id,p_actor_user_id::text,v_role,p_request_id,
        'audit.read','company',p_organization_id::text);
    select coalesce(jsonb_agg(to_jsonb(page)),'[]'::jsonb) into v_items
      from (
        select event.id,event.request_id,event.actor_id,event.actor_role,event.action,
               event.target_type,event.target_id,event.outcome,event.occurred_at
          from public.audit_events event
         where event.organization_id=p_organization_id
           and event.action not like '%.read'
           and (p_cursor_time is null or
                (event.occurred_at,event.id)<(p_cursor_time,p_cursor_id))
         order by event.occurred_at desc,event.id desc
         limit least(greatest(p_limit,1),100)+1
      ) page;
    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;

create or replace function public.company_admin_set_member(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_role text,
    p_status text,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_target public.organization_members%rowtype; v_owner_count bigint;
        v_revoked_count integer;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    select * into v_target from public.organization_members
     where organization_id=p_organization_id and user_id=p_target_user_id for update;
    if v_target.user_id is null
       or v_actor_role not in ('company_owner','company_admin')
       or p_role not in ('company_owner','company_admin','member','billing_admin')
       or p_status not in ('active','disabled','removed')
       or (v_actor_role='company_admin' and
           (v_target.role in ('company_owner','company_admin') or
            p_role in ('company_owner','company_admin'))) then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,'member.change.denied','member',
            p_target_user_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    if v_target.role='company_owner' and
       (p_role <> 'company_owner' or p_status <> 'active') then
        perform 1 from public.organization_members
         where organization_id=p_organization_id and role='company_owner'
           and status='active' for update;
        select count(*) into v_owner_count from public.organization_members
         where organization_id=p_organization_id and role='company_owner'
           and status='active';
        if v_owner_count <= 1 then
            perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
                v_actor_role,p_request_id,'member.change.denied','member',
                p_target_user_id::text,'denied');
            return jsonb_build_object('ok',false,'code','last_owner');
        end if;
    end if;
    update public.organization_members
       set role=p_role,status=p_status,updated_at=now(),
           disabled_at=case when p_status='disabled' then now() else null end,
           removed_at=case when p_status='removed' then now() else null end
     where organization_id=p_organization_id and user_id=p_target_user_id;
    -- A membership-ending transition revokes the target's own session and
    -- device credentials while the namespace lock is still held. Dashboard
    -- sessions are already blocked live by the membership checks, but a
    -- `bvx login` DEVICE key carries proxy:invoke / usage:write /
    -- customers:import / repositories:register / installations:register and
    -- authenticates with no live-membership read, so without this UPDATE a
    -- departed engineer's device credential survives removal indefinitely
    -- (device rows are inserted with expires_at NULL and the max-age knob
    -- defaults to no expiry). created_by is the activating human where
    -- recorded; rows minted by the hosted activation RPC before (and after)
    -- created_by plumbing are matched through their append-only
    -- 'device_key.activated' activation event instead, whose actor is the
    -- same approving human. Role-only changes deliberately do NOT revoke:
    -- dashboard-session authority is resolved from the LIVE role and device
    -- scopes are role-independent, so revocation there would only churn
    -- sessions.
    if p_status in ('disabled','removed') then
        update public.api_keys credential
           set revoked_at=now()
         where credential.organization_id=p_organization_id
           and credential.revoked_at is null
           and credential.key_type in ('dashboard_session','device')
           and (credential.created_by=p_target_user_id
                or (credential.key_type='device'
                    and credential.created_by is null
                    and exists (
                        select 1 from public.audit_events activation
                         where activation.organization_id=p_organization_id
                           and activation.action='device_key.activated'
                           and activation.target_id=credential.id::text
                           and activation.actor_user_id=p_target_user_id)));
        get diagnostics v_revoked_count = row_count;
        if v_revoked_count > 0 then
            perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
                v_actor_role,p_request_id,'member.credentials_revoked','member',
                p_target_user_id::text,'committed');
        end if;
    end if;
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'member.changed','member',p_target_user_id::text,'committed');
    -- 'member.changed' records that a permission changed but not what it changed
    -- from or to, and audit_events cannot carry details: validate_audit_event_insert
    -- rejects any row with `details <> '{}'` (202607170005:176-183), which is the
    -- enforcement point for the whole content-free policy and is deliberately not
    -- relaxed here. Both transitions are low-cardinality (4 roles, 3 statuses), so
    -- they are encoded in the `action` field, which already has a tight
    -- ^[a-z0-9_.-]{3,100}$ regex. These are ADDITIONAL events: 'member.changed'
    -- still fires, so nothing pinned to it changes meaning.
    if v_target.role is distinct from p_role then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            v_actor_role,p_request_id,
            'member.role_changed.'||v_target.role||'_to_'||p_role,
            'member',p_target_user_id::text,'committed');
    end if;
    if v_target.status is distinct from p_status then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            v_actor_role,p_request_id,
            'member.status_changed.'||v_target.status||'_to_'||p_status,
            'member',p_target_user_id::text,'committed');
    end if;
    return jsonb_build_object('ok',true,'id',p_target_user_id,'role',p_role,'status',p_status);
end;
$$;

-- One-time backfill for the revoke above: the hosted activation RPC
-- (consume_bvx_device_idempotent, latest body in 202607280005) inserts device
-- keys WITHOUT created_by, so existing production device rows carry NULL there
-- and a created_by-only revoke would match nothing. The activating human is
-- recoverable from the append-only 'device_key.activated' audit event, whose
-- actor is the approving (= holding) user and whose target_id is the api_keys
-- id. Idempotent: only NULL created_by rows are touched, earliest activation
-- event wins.
update public.api_keys credential
   set created_by = activation.actor_user_id
  from (
    select distinct on (event.organization_id, event.target_id)
           event.organization_id, event.target_id, event.actor_user_id
      from public.audit_events event
     where event.action='device_key.activated'
       and event.actor_user_id is not null
     order by event.organization_id, event.target_id, event.occurred_at asc, event.id asc
  ) activation
 where credential.key_type='device'
   and credential.created_by is null
   and credential.organization_id=activation.organization_id
   and credential.id::text=activation.target_id;

create or replace function public.company_admin_create_service_account(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_service_account_id uuid,
    p_name text,
    p_environment text,
    p_scopes text[],
    p_key_hash text,
    p_key_prefix text,
    p_expires_at timestamptz,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_actor_role text;
    v_account public.service_accounts%rowtype;
    v_billing_owner_id uuid;
    v_key_id uuid;
    v_scope text;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);
    select billing_owner_id into v_billing_owner_id
      from public.organizations
     where id=p_organization_id
     for update;

    if v_actor_role not in ('company_owner','company_admin')
       or v_billing_owner_id is null
       or char_length(trim(p_name)) not between 1 and 100
       or p_environment !~ '^[A-Za-z0-9._-]{1,32}$'
       or cardinality(p_scopes) not between 1 and 12
       or not (p_scopes <@ array[
            'proxy:invoke','usage:write','usage:read_own','customer:route',
            'customer:auto_provision','customers:import','repositories:register',
            'installations:register','provider:read','provider:manage',
            'jobs:create','jobs:read','jobs:cancel']::text[])
       or p_key_hash !~ '^[0-9a-f]{64}$'
       or char_length(p_key_prefix) not between 4 and 16
       or p_expires_at is null
       or p_expires_at <= now()
       or p_expires_at > now()+interval '365 days'
       or (select count(*) from public.service_accounts
            where organization_id=p_organization_id and status='active'
              and (expires_at is null or expires_at>now())) >= 100 then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'service_account.create.denied','service_account',
            p_service_account_id::text,'denied');
        return jsonb_build_object(
            'ok',false,'code','forbidden_or_limit');
    end if;

    insert into public.service_accounts(
        id,organization_id,name,environment,scopes,created_by,expires_at
    ) values (
        p_service_account_id,p_organization_id,trim(p_name),p_environment,
        p_scopes,p_actor_user_id,p_expires_at
    ) returning * into v_account;

    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,service_account_id,
        key_type,scopes,environment,key_prefix,expires_at,created_by
    ) values (
        p_key_hash,v_account.name,now(),v_billing_owner_id::text,
        p_organization_id,v_account.id,'organization_service',
        v_account.scopes,v_account.environment,p_key_prefix,
        v_account.expires_at,p_actor_user_id
    ) returning id into v_key_id;

    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        'service_account.created','service_account',v_account.id::text,
        'committed');
    -- 'service_account.created' omitted the granted authority, which is the one
    -- fact the event exists to record. The scope allowlist is a fixed 13 values
    -- (202607170005:565-568), so one content-free event per granted scope stays
    -- finite and target_id remains inside ^[A-Za-z0-9._:-]{1,200}$.
    foreach v_scope in array v_account.scopes loop
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'service_account.scope_granted','service_account',
            v_account.id::text||':'||v_scope,'committed');
    end loop;
    return jsonb_build_object(
        'ok',true,'id',v_account.id,'name',v_account.name,
        'environment',v_account.environment,'scopes',v_account.scopes,
        'status',v_account.status,'expires_at',v_account.expires_at,
        'key_id',v_key_id,'prefix',p_key_prefix);
exception when unique_violation then
    -- PL/pgSQL rolls back both inserts before entering this handler. Persist a
    -- content-free denial without ever leaving a keyless service account.
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,
        coalesce(v_actor_role,'none'),p_request_id,
        'service_account.create.denied','service_account',
        p_service_account_id::text,'denied');
    return jsonb_build_object('ok',false,'code','duplicate');
end;
$$;

revoke all on function public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)
    to service_role;
revoke all on function public.company_admin_set_member(uuid,uuid,uuid,text,text,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_set_member(uuid,uuid,uuid,text,text,text)
    to service_role;
revoke all on function public.company_admin_create_service_account(
    uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text
) from public, anon, authenticated;
grant execute on function public.company_admin_create_service_account(
    uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text
) to service_role;

comment on function public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text) is
    'Audit log read: records one deduplicated audit.read event per request and excludes read events from the page so the log cannot feed on itself.';
comment on function public.append_company_audit_read(uuid,text,text,text,text,text,text) is
    'Content-free read-evidence append, idempotent per (organization, request_id, action). Mutations and denials continue to use append_company_audit.';

commit;
