-- Company administration boundary.
-- Canonical roles are company_owner, company_admin, member, and billing_admin.
-- All privileged mutations are service-role RPCs that re-authorize against the
-- locked membership row and append a content-free audit event in one transaction.

create extension if not exists pgcrypto;

-- Normalize the provisional tenancy roles introduced by migration 001.
alter table public.organization_members drop constraint if exists organization_members_role_check;
update public.organization_members
   set role = case role
       when 'owner' then 'company_owner'
       when 'admin' then 'company_admin'
       when 'billing' then 'billing_admin'
       else role
   end
 where role in ('owner', 'admin', 'billing');
alter table public.organization_members
    add constraint organization_members_role_check
    check (role in ('company_owner', 'company_admin', 'member', 'billing_admin'));
alter table public.organization_members
    add column if not exists status text not null default 'active';
alter table public.organization_members
    add column if not exists updated_at timestamptz not null default now();
alter table public.organization_members
    add column if not exists disabled_at timestamptz;
alter table public.organization_members
    add column if not exists removed_at timestamptz;
alter table public.organization_members drop constraint if exists organization_members_status_check;
alter table public.organization_members
    add constraint organization_members_status_check
    check (status in ('active', 'disabled', 'removed'));
create unique index if not exists organization_members_tenant_identity_idx
    on public.organization_members(organization_id, user_id);
create index if not exists organization_members_tenant_page_idx
    on public.organization_members(organization_id, created_at desc, user_id desc);

-- Replace the provisional bootstrap RPC so organizations created after this
-- migration never attempt to insert a retired legacy role.
create or replace function public.ensure_enterprise_organization(
    p_user_id uuid,
    p_name text default 'My organization'
) returns table(id uuid, name text, role text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare organization public.organizations%rowtype;
begin
    insert into public.organizations(name, legacy_owner_id, billing_owner_id)
    values (coalesce(nullif(p_name, ''), 'My organization'), p_user_id::text, p_user_id)
    on conflict (legacy_owner_id) do update set legacy_owner_id=excluded.legacy_owner_id
    returning * into organization;
    insert into public.organization_members(organization_id,user_id,role,status)
    values(organization.id,p_user_id,'company_owner','active')
    on conflict (organization_id,user_id) do nothing;
    return query select organization.id,organization.name,member.role
      from public.organization_members member
     where member.organization_id=organization.id and member.user_id=p_user_id;
end;
$$;
revoke all on function public.ensure_enterprise_organization(uuid,text)
    from public, anon, authenticated;
grant execute on function public.ensure_enterprise_organization(uuid,text) to service_role;

-- Invitations contain only keyed email lookup material and a token digest. The
-- raw email and raw token are never persisted. Delivery is an application concern.
create table if not exists public.organization_invitations (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete cascade,
    email_lookup_hash text not null check (email_lookup_hash ~ '^[0-9a-f]{64}$'),
    token_hash text not null unique check (token_hash ~ '^[0-9a-f]{64}$'),
    role text not null check (role in ('company_admin', 'member', 'billing_admin')),
    status text not null default 'pending'
        check (status in ('pending', 'accepted', 'cancelled', 'expired')),
    invited_by uuid not null references auth.users(id) on delete restrict,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    accepted_at timestamptz,
    cancelled_at timestamptz,
    accepted_by uuid references auth.users(id) on delete restrict,
    unique (organization_id, id),
    check (expires_at <= created_at + interval '7 days')
);
create unique index if not exists organization_invitations_one_pending_idx
    on public.organization_invitations(organization_id, email_lookup_hash)
    where status = 'pending';
create index if not exists organization_invitations_tenant_page_idx
    on public.organization_invitations(organization_id, created_at desc, id desc);
alter table public.organization_invitations enable row level security;

-- Service accounts have explicit bounded scopes and lifecycle state. API key
-- rows retain only SHA-256 digests; raw one-time credentials never cross SQL.
alter table public.service_accounts
    add column if not exists scopes text[] not null default array['proxy:invoke']::text[];
alter table public.service_accounts
    add column if not exists status text not null default 'active';
alter table public.service_accounts
    add column if not exists expires_at timestamptz;
alter table public.service_accounts
    add column if not exists revoked_at timestamptz;
alter table public.service_accounts
    add column if not exists updated_at timestamptz not null default now();
alter table public.service_accounts drop constraint if exists service_accounts_status_check;
alter table public.service_accounts
    add constraint service_accounts_status_check check (status in ('active', 'revoked'));
alter table public.service_accounts drop constraint if exists service_accounts_scope_check;
alter table public.service_accounts
    add constraint service_accounts_scope_check check (
        cardinality(scopes) between 1 and 12
        and scopes <@ array[
            'proxy:invoke', 'usage:write', 'usage:read_own',
            'customer:route', 'customer:auto_provision', 'customers:import',
            'repositories:register', 'installations:register',
            'provider:read', 'provider:manage',
            'jobs:create', 'jobs:read', 'jobs:cancel'
        ]::text[]
    );
create index if not exists service_accounts_tenant_page_idx
    on public.service_accounts(organization_id, created_at desc, id desc);
create index if not exists api_keys_service_active_idx
    on public.api_keys(organization_id, service_account_id, created desc, id desc)
    where revoked_at is null;

-- Upgrade the provisional audit table without rewriting or discarding evidence.
alter table public.audit_events add column if not exists request_id text;
alter table public.audit_events add column if not exists actor_id text;
alter table public.audit_events add column if not exists actor_role text;
alter table public.audit_events add column if not exists outcome text;
update public.audit_events
   set request_id = coalesce(request_id, 'legacy-' || id::text),
       actor_id = coalesce(actor_id, actor_user_id::text, 'system'),
       actor_role = coalesce(actor_role, 'legacy'),
       outcome = coalesce(outcome, 'committed')
 where request_id is null or actor_id is null or actor_role is null or outcome is null;
alter table public.audit_events alter column request_id set not null;
alter table public.audit_events alter column actor_id set not null;
alter table public.audit_events alter column actor_role set not null;
alter table public.audit_events alter column outcome set not null;
-- Compatibility defaults keep pre-migration content-free audit producers
-- append-only while they adopt explicit correlation. Company administration
-- RPCs always supply all four fields and never use these fallbacks.
alter table public.audit_events alter column request_id set default gen_random_uuid()::text;
alter table public.audit_events alter column actor_id set default 'system';
alter table public.audit_events alter column actor_role set default 'legacy';
alter table public.audit_events alter column outcome set default 'committed';
alter table public.audit_events drop constraint if exists audit_events_outcome_check;
alter table public.audit_events
    add constraint audit_events_outcome_check check (outcome in ('committed', 'denied'));
create unique index if not exists audit_events_event_id_idx on public.audit_events(id);
create index if not exists audit_events_tenant_cursor_idx
    on public.audit_events(organization_id, occurred_at desc, id desc);

-- A row policy is insufficient because service_role bypasses RLS. This trigger
-- rejects UPDATE, DELETE, and TRUNCATE for every database role, including the
-- service role. Only a database owner intentionally disabling/dropping it can
-- change evidence, which is a separately monitored break-glass event.
create or replace function public.reject_audit_event_mutation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    raise exception 'audit_events is append-only' using errcode = '55000';
end;
$$;
revoke all on function public.reject_audit_event_mutation() from public, anon, authenticated, service_role;
create or replace function public.validate_audit_event_insert()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if new.details <> '{}'::jsonb
       or new.request_id !~ '^[A-Za-z0-9._:-]{8,128}$'
       or new.actor_id !~ '^[A-Za-z0-9._:-]{1,128}$'
       or new.actor_role not in (
           'company_owner','company_admin','member','billing_admin',
           'brevitas_admin','service_account','system','legacy','none')
       or new.action !~ '^[a-z0-9_.-]{3,100}$'
       or new.target_type !~ '^[a-z0-9_.-]{1,50}$'
       or new.target_id !~ '^[A-Za-z0-9._:-]{1,200}$'
       or new.actor_key_hash is not null
       or new.actor_id ~* '^[0-9a-f]{64}$'
       or new.target_id ~* '^[0-9a-f]{64}$'
       or (new.actor_id || ':' || new.target_id) ~* '@|(^|[._:-])(bvt|sk|rk|pk|whsec|sb_secret|xox[baprs]|gh[opusr])[_-]'
       or (new.actor_id || ':' || new.target_id) ~* '(^|[._:-])(secret|password|token|authorization|api[_-]?key)([._:-]|$)'
       or new.outcome not in ('committed','denied') then
        raise exception 'audit event violates content-free schema' using errcode='22023';
    end if;
    if new.actor_id='system' and new.actor_user_id is not null then
        new.actor_id := new.actor_user_id::text;
    end if;
    return new;
end;
$$;
revoke all on function public.validate_audit_event_insert()
    from public, anon, authenticated, service_role;
drop trigger if exists audit_events_validate_insert on public.audit_events;
create trigger audit_events_validate_insert
    before insert on public.audit_events
    for each row execute function public.validate_audit_event_insert();
drop trigger if exists audit_events_reject_update_delete on public.audit_events;
create trigger audit_events_reject_update_delete
    before update or delete on public.audit_events
    for each row execute function public.reject_audit_event_mutation();
drop trigger if exists audit_events_reject_truncate on public.audit_events;
create trigger audit_events_reject_truncate
    before truncate on public.audit_events
    for each statement execute function public.reject_audit_event_mutation();

create or replace function public.append_company_audit(
    p_organization_id uuid,
    p_actor_id text,
    p_actor_role text,
    p_request_id text,
    p_action text,
    p_target_type text,
    p_target_id text,
    p_outcome text default 'committed'
) returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_id bigint;
begin
    if p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$'
       or p_action !~ '^[a-z0-9_.-]{3,100}$'
       or p_target_type !~ '^[a-z0-9_.-]{1,50}$'
       or char_length(p_target_id) not between 1 and 200
       or p_outcome not in ('committed', 'denied') then
        raise exception 'invalid content-free audit fields' using errcode = '22023';
    end if;
    insert into public.audit_events(
        organization_id, actor_user_id, actor_id, actor_role, request_id,
        action, target_type, target_id, outcome, details
    ) values (
        p_organization_id,
        case when p_actor_id ~ '^[0-9a-fA-F-]{36}$' then p_actor_id::uuid else null end,
        p_actor_id, p_actor_role, p_request_id,
        p_action, p_target_type, p_target_id, p_outcome, '{}'::jsonb
    ) returning id into v_id;
    return v_id;
end;
$$;
revoke all on function public.append_company_audit(uuid,text,text,text,text,text,text,text)
    from public, anon, authenticated;
grant execute on function public.append_company_audit(uuid,text,text,text,text,text,text,text)
    to service_role;

create or replace function public.company_role_permissions(p_role text)
returns text[]
language sql
immutable
parallel safe
as $$
    select case p_role
      when 'company_owner' then array[
        'company:read','members:read','members:invite','members:manage','owners:manage',
        'service_accounts:read','service_accounts:manage','billing:manage','audit:read'
      ]::text[]
      when 'company_admin' then array[
        'company:read','members:read','members:invite','members:manage',
        'service_accounts:read','service_accounts:manage','audit:read'
      ]::text[]
      when 'billing_admin' then array[
        'company:read','members:read','billing:manage','audit:read'
      ]::text[]
      when 'member' then array['company:read','members:read']::text[]
      else array[]::text[]
    end;
$$;
revoke all on function public.company_role_permissions(text) from public, anon, authenticated;
grant execute on function public.company_role_permissions(text) to service_role;

-- Returns the locked, active role. RPC callers never trust a role supplied by HTTP.
create or replace function public.lock_company_actor_role(
    p_organization_id uuid, p_actor_user_id uuid
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_role text;
begin
    select role into v_role
      from public.organization_members
     where organization_id = p_organization_id
       and user_id = p_actor_user_id
       and status = 'active'
     for update;
    return v_role;
end;
$$;
revoke all on function public.lock_company_actor_role(uuid,uuid)
    from public, anon, authenticated;
grant execute on function public.lock_company_actor_role(uuid,uuid) to service_role;

-- One organization-row lock serializes per-company invitation and service
-- account caps across every API/worker replica.
create or replace function public.lock_company_admin_namespace(p_organization_id uuid)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    perform 1 from public.organizations
     where id=p_organization_id
     for update;
    if not found then
        raise exception 'organization not found' using errcode='P0002';
    end if;
end;
$$;
revoke all on function public.lock_company_admin_namespace(uuid)
    from public, anon, authenticated;
grant execute on function public.lock_company_admin_namespace(uuid) to service_role;

drop function if exists public.company_admin_invite_member(
    uuid,uuid,text,text,text,timestamptz,text);
create or replace function public.company_admin_invite_member(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_invitation_id uuid,
    p_role text,
    p_email_lookup_hash text,
    p_token_hash text,
    p_expires_at timestamptz,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_invitation public.organization_invitations%rowtype;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(p_organization_id, p_actor_user_id);
    if v_actor_role not in ('company_owner', 'company_admin')
       or p_role not in ('company_admin', 'member', 'billing_admin') then
        perform public.append_company_audit(p_organization_id, p_actor_user_id::text,
            coalesce(v_actor_role, 'none'), p_request_id, 'member.invite.denied',
            'invitation', p_invitation_id::text, 'denied');
        return jsonb_build_object('ok', false, 'code', 'forbidden');
    end if;
    -- Stale rows are transitioned while holding the namespace lock, before the
    -- cap is counted, so concurrent replicas cannot both observe a free slot.
    update public.organization_invitations set status='expired'
     where organization_id=p_organization_id and status='pending' and expires_at <= now();
    if p_expires_at <= now() or p_expires_at > now() + interval '7 days'
       or (select count(*) from public.organization_invitations
            where organization_id=p_organization_id and status='pending'
              and expires_at > now()) >= 100 then
        perform public.append_company_audit(p_organization_id, p_actor_user_id::text,
            v_actor_role, p_request_id, 'member.invite.denied', 'invitation',
            p_invitation_id::text, 'denied');
        return jsonb_build_object('ok', false, 'code', 'limit');
    end if;
    insert into public.organization_invitations(
        id,organization_id,email_lookup_hash,token_hash,role,invited_by,expires_at
    ) values (
        p_invitation_id,p_organization_id,p_email_lookup_hash,p_token_hash,p_role,p_actor_user_id,p_expires_at
    ) returning * into v_invitation;
    perform public.append_company_audit(p_organization_id, p_actor_user_id::text,
        v_actor_role, p_request_id, 'member.invited', 'invitation',
        v_invitation.id::text, 'committed');
    return jsonb_build_object('ok', true, 'id', v_invitation.id,
        'role', v_invitation.role, 'status', v_invitation.status,
        'expires_at', v_invitation.expires_at);
exception when unique_violation then
    perform public.append_company_audit(p_organization_id, p_actor_user_id::text,
        coalesce(v_actor_role, 'none'), p_request_id, 'member.invite.denied',
        'invitation', p_invitation_id::text, 'denied');
    return jsonb_build_object('ok', false, 'code', 'already_invited');
end;
$$;
revoke all on function public.company_admin_invite_member(uuid,uuid,uuid,text,text,text,timestamptz,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_invite_member(uuid,uuid,uuid,text,text,text,timestamptz,text)
    to service_role;

create or replace function public.company_admin_cancel_invitation(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_invitation_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_changed bigint;
begin
    v_actor_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_actor_role not in ('company_owner','company_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,'member.invitation.cancel.denied',
            'invitation',p_invitation_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    update public.organization_invitations
       set status='cancelled',cancelled_at=now()
     where organization_id=p_organization_id and id=p_invitation_id
       and status='pending' and expires_at>now();
    get diagnostics v_changed = row_count;
    if v_changed=0 then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            v_actor_role,p_request_id,'member.invitation.cancel.denied','invitation',
            p_invitation_id::text,'denied');
        return jsonb_build_object('ok',false,'code','not_found');
    end if;
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'member.invitation.cancelled','invitation',
        p_invitation_id::text,'committed');
    return jsonb_build_object('ok',true,'id',p_invitation_id,'status','cancelled');
end;
$$;
revoke all on function public.company_admin_cancel_invitation(uuid,uuid,uuid,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_cancel_invitation(uuid,uuid,uuid,text)
    to service_role;

drop function if exists public.company_admin_accept_invitation(uuid,text,text);
create or replace function public.company_admin_accept_invitation(
    p_actor_user_id uuid,
    p_invitee_lookup_hash text,
    p_token_hash text,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_invitation public.organization_invitations%rowtype;
        v_existing public.organization_members%rowtype;
begin
    select * into v_invitation from public.organization_invitations
     where token_hash=p_token_hash for update;
    if v_invitation.id is null then
        return jsonb_build_object('ok', false, 'code', 'invalid_invitation');
    end if;
    if v_invitation.status <> 'pending'
       or v_invitation.expires_at <= now()
       or p_invitee_lookup_hash !~ '^[0-9a-f]{64}$'
       or v_invitation.email_lookup_hash <> p_invitee_lookup_hash then
        perform public.append_company_audit(v_invitation.organization_id,
            p_actor_user_id::text,'none',p_request_id,
            'member.invitation.accept.denied','invitation',
            v_invitation.id::text,'denied');
       return jsonb_build_object('ok',false,'code','wrong_invitee_or_replay');
    end if;
    perform public.lock_company_admin_namespace(v_invitation.organization_id);
    select * into v_existing from public.organization_members
     where organization_id=v_invitation.organization_id
       and user_id=p_actor_user_id
     for update;
    if v_existing.user_id is not null then
        perform public.append_company_audit(v_invitation.organization_id,
            p_actor_user_id::text,v_existing.role,p_request_id,
            'member.invitation.accept.denied','invitation',
            v_invitation.id::text,'denied');
        return jsonb_build_object('ok',false,'code','existing_member');
    end if;
    insert into public.organization_members(organization_id,user_id,role,status)
    values(v_invitation.organization_id,p_actor_user_id,v_invitation.role,'active');
    update public.organization_invitations
       set status='accepted',accepted_at=now(),accepted_by=p_actor_user_id
     where id=v_invitation.id;
    perform public.append_company_audit(v_invitation.organization_id,p_actor_user_id::text,
        v_invitation.role,p_request_id,'member.invitation.accepted','invitation',
        v_invitation.id::text,'committed');
    return jsonb_build_object('ok',true,'organization_id',v_invitation.organization_id,
        'role',v_invitation.role);
end;
$$;
revoke all on function public.company_admin_accept_invitation(uuid,text,text,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_accept_invitation(uuid,text,text,text) to service_role;

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
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'member.changed','member',p_target_user_id::text,'committed');
    return jsonb_build_object('ok',true,'id',p_target_user_id,'role',p_role,'status',p_status);
end;
$$;
revoke all on function public.company_admin_set_member(uuid,uuid,uuid,text,text,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_set_member(uuid,uuid,uuid,text,text,text)
    to service_role;

drop function if exists public.company_admin_create_service_account(
    uuid,uuid,text,text,text[],timestamptz,text);
create or replace function public.company_admin_create_service_account(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_service_account_id uuid,
    p_name text,
    p_environment text,
    p_scopes text[],
    p_expires_at timestamptz,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_account public.service_accounts%rowtype;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_actor_role not in ('company_owner','company_admin')
       or char_length(trim(p_name)) not between 1 and 100
       or p_environment !~ '^[A-Za-z0-9._-]{1,32}$'
       or cardinality(p_scopes) not between 1 and 12
       or not (p_scopes <@ array[
            'proxy:invoke','usage:write','usage:read_own','customer:route',
            'customer:auto_provision','customers:import','repositories:register',
            'installations:register','provider:read','provider:manage',
            'jobs:create','jobs:read','jobs:cancel']::text[])
       or p_expires_at is null
       or p_expires_at <= now() or p_expires_at > now()+interval '365 days'
       or (select count(*) from public.service_accounts
            where organization_id=p_organization_id and status='active'
              and (expires_at is null or expires_at>now())) >= 100 then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,'service_account.create.denied',
            'service_account',p_service_account_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden_or_limit');
    end if;
    insert into public.service_accounts(
        id,organization_id,name,environment,scopes,created_by,expires_at
    ) values (
        p_service_account_id,p_organization_id,trim(p_name),p_environment,p_scopes,p_actor_user_id,p_expires_at
    ) returning * into v_account;
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'service_account.created','service_account',
        v_account.id::text,'committed');
    return jsonb_build_object('ok',true,'id',v_account.id,'name',v_account.name,
        'environment',v_account.environment,'scopes',v_account.scopes,
        'status',v_account.status,'expires_at',v_account.expires_at);
exception when unique_violation then
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        coalesce(v_actor_role,'none'),p_request_id,'service_account.create.denied',
        'service_account',p_service_account_id::text,'denied');
    return jsonb_build_object('ok',false,'code','duplicate');
end;
$$;
revoke all on function public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_create_service_account(uuid,uuid,uuid,text,text,text[],timestamptz,text)
    to service_role;

create or replace function public.company_admin_rotate_service_key(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_service_account_id uuid,
    p_key_hash text,
    p_key_prefix text,
    p_expires_at timestamptz,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_account public.service_accounts%rowtype; v_key_id uuid;
        v_key_expiry timestamptz;
begin
    v_actor_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    select * into v_account from public.service_accounts
     where organization_id=p_organization_id and id=p_service_account_id for update;
    if v_actor_role not in ('company_owner','company_admin')
       or v_account.id is null or v_account.status <> 'active'
       or v_account.revoked_at is not null
       or v_account.expires_at is null or v_account.expires_at <= now()
       or p_key_hash !~ '^[0-9a-f]{64}$'
       or char_length(p_key_prefix) not between 4 and 16
       or p_expires_at is null or p_expires_at <= now()
       or p_expires_at > now()+interval '365 days' then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,'service_key.rotate.denied',
            'service_account',p_service_account_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    v_key_expiry := least(p_expires_at,v_account.expires_at);
    update public.api_keys set revoked_at=now()
     where organization_id=p_organization_id
       and service_account_id=p_service_account_id and revoked_at is null;
    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,service_account_id,key_type,
        scopes,environment,key_prefix,expires_at,created_by
    ) values (
        p_key_hash,v_account.name,now(),p_actor_user_id::text,p_organization_id,
        p_service_account_id,'organization_service',v_account.scopes,
        v_account.environment,p_key_prefix,v_key_expiry,p_actor_user_id
    ) returning id into v_key_id;
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'service_key.rotated','service_account',
        p_service_account_id::text,'committed');
    return jsonb_build_object('ok',true,'key_id',v_key_id,'prefix',p_key_prefix,
        'expires_at',v_key_expiry);
end;
$$;
revoke all on function public.company_admin_rotate_service_key(uuid,uuid,uuid,text,text,timestamptz,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_rotate_service_key(uuid,uuid,uuid,text,text,timestamptz,text)
    to service_role;

-- Runtime authentication contract for W1. A key-table hit alone is never
-- authoritative: the service account is joined on the composite tenant key and
-- both expiries/revocation states must be live.
create or replace function public.service_key_authorization(p_key_hash text)
returns table(
    key_hash text, organization_id uuid, service_account_id uuid,
    key_type text, scopes text[], environment text,
    key_expires_at timestamptz, account_expires_at timestamptz
)
language sql
security definer
set search_path = public, pg_temp
as $$
    select credential.key_hash,credential.organization_id,
           credential.service_account_id,credential.key_type,
           credential.scopes,credential.environment,
           credential.expires_at,account.expires_at
      from public.api_keys credential
      join public.service_accounts account
        on account.organization_id=credential.organization_id
       and account.id=credential.service_account_id
     where credential.key_hash=p_key_hash
       and credential.key_type='organization_service'
       and credential.revoked_at is null
       and credential.expires_at is not null and credential.expires_at>now()
       and account.status='active' and account.revoked_at is null
       and account.expires_at is not null and account.expires_at>now()
       and credential.expires_at<=account.expires_at
     limit 1;
$$;
revoke all on function public.service_key_authorization(text)
    from public, anon, authenticated;
grant execute on function public.service_key_authorization(text) to service_role;

create or replace function public.company_admin_revoke_service_account(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_service_account_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_actor_role text; v_changed bigint;
begin
    v_actor_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_actor_role not in ('company_owner','company_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,'service_account.revoke.denied',
            'service_account',p_service_account_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    update public.service_accounts set status='revoked',revoked_at=now(),updated_at=now()
     where organization_id=p_organization_id and id=p_service_account_id and status='active';
    get diagnostics v_changed = row_count;
    if v_changed = 0 then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            v_actor_role,p_request_id,'service_account.revoke.denied','service_account',
            p_service_account_id::text,'denied');
        return jsonb_build_object('ok',false,'code','not_found');
    end if;
    update public.api_keys set revoked_at=now()
     where organization_id=p_organization_id
       and service_account_id=p_service_account_id and revoked_at is null;
    perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
        v_actor_role,p_request_id,'service_account.revoked','service_account',
        p_service_account_id::text,'committed');
    return jsonb_build_object('ok',true,'id',p_service_account_id,'status','revoked');
end;
$$;
revoke all on function public.company_admin_revoke_service_account(uuid,uuid,uuid,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_revoke_service_account(uuid,uuid,uuid,text)
    to service_role;

-- Each list RPC performs authorization and keyset selection in one database
-- transaction. No service-role table GET can race a membership revocation.
create or replace function public.company_admin_members_page(
    p_organization_id uuid, p_actor_user_id uuid,
    p_cursor_time timestamptz, p_cursor_id uuid, p_limit integer,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_role text; v_items jsonb;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_role not in ('company_owner','company_admin','member','billing_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_role,'none'),p_request_id,'members.read.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    select coalesce(jsonb_agg(to_jsonb(page)),'[]'::jsonb) into v_items
      from (
        select member.user_id as id,member.role,member.status,member.created_at
          from public.organization_members member
         where member.organization_id=p_organization_id
           and (p_cursor_time is null or
                (member.created_at,member.user_id)<(p_cursor_time,p_cursor_id))
         order by member.created_at desc,member.user_id desc
         limit least(greatest(p_limit,1),100)+1
      ) page;
    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;
revoke all on function public.company_admin_members_page(uuid,uuid,timestamptz,uuid,integer,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_members_page(uuid,uuid,timestamptz,uuid,integer,text)
    to service_role;

create or replace function public.company_admin_invitations_page(
    p_organization_id uuid, p_actor_user_id uuid,
    p_cursor_time timestamptz, p_cursor_id uuid, p_limit integer,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_role text; v_items jsonb;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_role not in ('company_owner','company_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_role,'none'),p_request_id,'invitations.read.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    update public.organization_invitations set status='expired'
     where organization_id=p_organization_id and status='pending' and expires_at<=now();
    select coalesce(jsonb_agg(to_jsonb(page)),'[]'::jsonb) into v_items
      from (
        select invitation.id,invitation.role,invitation.status,
               invitation.expires_at,invitation.created_at
          from public.organization_invitations invitation
         where invitation.organization_id=p_organization_id
           and (p_cursor_time is null or
                (invitation.created_at,invitation.id)<(p_cursor_time,p_cursor_id))
         order by invitation.created_at desc,invitation.id desc
         limit least(greatest(p_limit,1),100)+1
      ) page;
    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;
revoke all on function public.company_admin_invitations_page(uuid,uuid,timestamptz,uuid,integer,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_invitations_page(uuid,uuid,timestamptz,uuid,integer,text)
    to service_role;

create or replace function public.company_admin_service_accounts_page(
    p_organization_id uuid, p_actor_user_id uuid,
    p_cursor_time timestamptz, p_cursor_id uuid, p_limit integer,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare v_role text; v_items jsonb;
begin
    v_role := public.lock_company_actor_role(p_organization_id,p_actor_user_id);
    if v_role not in ('company_owner','company_admin') then
        perform public.append_company_audit(p_organization_id,p_actor_user_id::text,
            coalesce(v_role,'none'),p_request_id,'service_accounts.read.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    select coalesce(jsonb_agg(to_jsonb(page)),'[]'::jsonb) into v_items
      from (
        select account.id,account.name,account.environment,account.scopes,
               case when account.expires_at<=now() then 'revoked' else account.status end as status,
               account.expires_at,account.revoked_at,account.created_at
          from public.service_accounts account
         where account.organization_id=p_organization_id
           and (p_cursor_time is null or
                (account.created_at,account.id)<(p_cursor_time,p_cursor_id))
         order by account.created_at desc,account.id desc
         limit least(greatest(p_limit,1),100)+1
      ) page;
    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;
revoke all on function public.company_admin_service_accounts_page(uuid,uuid,timestamptz,uuid,integer,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_service_accounts_page(uuid,uuid,timestamptz,uuid,integer,text)
    to service_role;

-- Audit reads are also one-transaction authorization + keyset RPCs. The API
-- signs the (occurred_at,id) tuple; browsers store it without decoding.
drop function if exists public.company_admin_audit_page(
    uuid,uuid,timestamptz,bigint,integer);
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
    select coalesce(jsonb_agg(to_jsonb(page)),'[]'::jsonb) into v_items
      from (
        select event.id,event.request_id,event.actor_id,event.actor_role,event.action,
               event.target_type,event.target_id,event.outcome,event.occurred_at
          from public.audit_events event
         where event.organization_id=p_organization_id
           and (p_cursor_time is null or
                (event.occurred_at,event.id)<(p_cursor_time,p_cursor_id))
         order by event.occurred_at desc,event.id desc
         limit least(greatest(p_limit,1),100)+1
      ) page;
    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;
revoke all on function public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)
    from public, anon, authenticated;
grant execute on function public.company_admin_audit_page(uuid,uuid,timestamptz,bigint,integer,text)
    to service_role;

-- Rollback safety: run this first. It copies immutable evidence to an archive
-- schema before any administration object is removed. The operational rollback
-- commands and evidence-export verification are in docs/COMPANY_ADMINISTRATION.md.
create schema if not exists audit_evidence_archive;
revoke all on schema audit_evidence_archive from public, anon, authenticated, service_role;
create or replace function public.archive_company_administration_audit()
returns bigint
language plpgsql
security definer
set search_path = public, audit_evidence_archive, pg_temp
as $$
declare v_count bigint;
begin
    create table if not exists audit_evidence_archive.company_admin_audit
        (like public.audit_events including all);
    insert into audit_evidence_archive.company_admin_audit overriding system value
    select * from public.audit_events
    on conflict (id) do nothing;
    select count(*) into v_count from audit_evidence_archive.company_admin_audit;
    return v_count;
end;
$$;
revoke all on function public.archive_company_administration_audit()
    from public, anon, authenticated, service_role;
