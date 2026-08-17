-- Default-customer pin for organization_service keys (onboarding A1).
--
-- A single-tenant hosted key currently hard-400s on every proxy call that omits the
-- X-Brevitas-Customer-ID header — the single most common first-request failure. This adds
-- an OPTIONAL per-key pin: when a service account carries default_customer_external_id, a
-- header-less proxy call on its key resolves to that customer instead of 400ing. Keys left
-- unpinned (multi-tenant) are unchanged and still require the header.
--
-- Security: the pin is stored on the key-bound service_accounts row and returned only by
-- service_key_authorization (keyed by the presented key hash). The proxy prefers an
-- explicitly sent header over the pin; a null/empty pin reproduces today's behaviour
-- exactly. See api/server.py:_auth_context_for_key.
--
-- Degrade/deploy: application code omits p_default_customer_external_id when empty, so
-- multi-tenant creation still works if this migration lags the code. A single-tenant pin
-- (brevitas connect without --multi-tenant) requires this migration.

alter table public.service_accounts
    add column if not exists default_customer_external_id text not null default ''
        check (default_customer_external_id = ''
               or default_customer_external_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$');

-- service_key_authorization now also returns the pin. Return-table shape changes, so drop
-- and recreate (mirrors 202607200003).
drop function if exists public.service_key_authorization(text);
create function public.service_key_authorization(p_key_hash text)
returns table(
    key_hash text,
    owner_id text,
    organization_id uuid,
    service_account_id uuid,
    key_type text,
    scopes text[],
    environment text,
    key_expires_at timestamptz,
    account_expires_at timestamptz,
    default_customer_external_id text
)
language sql
security definer
set search_path = public, pg_temp
as $$
    select credential.key_hash,organization.billing_owner_id::text,
           credential.organization_id,credential.service_account_id,
           credential.key_type,credential.scopes,credential.environment,
           credential.expires_at,account.expires_at,
           coalesce(account.default_customer_external_id,'')
      from public.api_keys credential
      join public.service_accounts account
        on account.organization_id=credential.organization_id
       and account.id=credential.service_account_id
      join public.organizations organization
        on organization.id=credential.organization_id
     where credential.key_hash=p_key_hash
       and credential.key_type='organization_service'
       and organization.billing_owner_id is not null
       and credential.revoked_at is null
       and credential.expires_at is not null
       and credential.expires_at>now()
       and account.status='active'
       and account.revoked_at is null
       and account.expires_at is not null
       and account.expires_at>now()
       and credential.expires_at<=account.expires_at
     limit 1;
$$;
revoke all on function public.service_key_authorization(text)
    from public, anon, authenticated;
grant execute on function public.service_key_authorization(text)
    to service_role;

-- company_admin_create_service_account gains an optional pin parameter. The parameter list
-- changes, so drop the old signature and recreate (mirrors 202607200003's service key
-- authorization handling). Body is otherwise identical to 202607280022.
drop function if exists public.company_admin_create_service_account(
    uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text);
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
    p_request_id text,
    p_default_customer_external_id text default ''
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
    v_pin text := coalesce(trim(p_default_customer_external_id),'');
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
       or (v_pin <> '' and v_pin !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')
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
        id,organization_id,name,environment,scopes,created_by,expires_at,
        default_customer_external_id
    ) values (
        p_service_account_id,p_organization_id,trim(p_name),p_environment,
        p_scopes,p_actor_user_id,p_expires_at,v_pin
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
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,
        coalesce(v_actor_role,'none'),p_request_id,
        'service_account.create.denied','service_account',
        p_service_account_id::text,'denied');
    return jsonb_build_object('ok',false,'code','duplicate');
end;
$$;
revoke all on function public.company_admin_create_service_account(
    uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text,text
) from public, anon, authenticated;
grant execute on function public.company_admin_create_service_account(
    uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text,text
) to service_role;
