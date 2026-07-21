-- Make the organization's billing owner authoritative for service-account
-- credentials. Human creator/rotator identity remains in api_keys.created_by
-- and append-only company audit events; it is never reused as billable owner.

begin;

create or replace function public.enforce_service_key_billing_owner()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_billing_owner_id uuid;
begin
    if new.key_type <> 'organization_service' then
        return new;
    end if;

    select organization.billing_owner_id
      into v_billing_owner_id
      from public.service_accounts account
      join public.organizations organization
        on organization.id=account.organization_id
     where account.organization_id=new.organization_id
       and account.id=new.service_account_id;

    if v_billing_owner_id is null then
        raise exception using
            errcode='23514',
            message='organization service key requires an authoritative billing owner';
    end if;

    new.owner_id := v_billing_owner_id::text;
    return new;
end;
$$;
revoke all on function public.enforce_service_key_billing_owner()
    from public, anon, authenticated;

drop trigger if exists enforce_service_key_billing_owner on public.api_keys;
create trigger enforce_service_key_billing_owner
before insert or update of owner_id,organization_id,service_account_id,key_type
on public.api_keys
for each row execute function public.enforce_service_key_billing_owner();

-- Correct active credential metadata for future receipts. Usage and billing
-- ledger rows are retained as immutable historical evidence and are not
-- rewritten by this migration.
update public.api_keys credential
   set owner_id=organization.billing_owner_id::text
  from public.organizations organization
 where credential.key_type='organization_service'
   and credential.organization_id=organization.id
   and organization.billing_owner_id is not null
   and credential.owner_id is distinct from organization.billing_owner_id::text;

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
declare
    v_actor_role text;
    v_account public.service_accounts%rowtype;
    v_billing_owner_id uuid;
    v_key_id uuid;
    v_key_expiry timestamptz;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);
    select * into v_account
      from public.service_accounts
     where organization_id=p_organization_id
       and id=p_service_account_id
     for update;
    select billing_owner_id into v_billing_owner_id
      from public.organizations
     where id=p_organization_id
     for update;

    if v_actor_role not in ('company_owner','company_admin')
       or v_account.id is null or v_account.status <> 'active'
       or v_account.revoked_at is not null
       or v_account.expires_at is null or v_account.expires_at <= now()
       or v_billing_owner_id is null
       or p_key_hash !~ '^[0-9a-f]{64}$'
       or char_length(p_key_prefix) not between 4 and 16
       or p_expires_at is null or p_expires_at <= now()
       or p_expires_at > now()+interval '365 days' then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'service_key.rotate.denied','service_account',
            p_service_account_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;

    v_key_expiry := least(p_expires_at,v_account.expires_at);
    update public.api_keys set revoked_at=now()
     where organization_id=p_organization_id
       and service_account_id=p_service_account_id
       and revoked_at is null;
    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,service_account_id,
        key_type,scopes,environment,key_prefix,expires_at,created_by
    ) values (
        p_key_hash,v_account.name,now(),v_billing_owner_id::text,
        p_organization_id,p_service_account_id,'organization_service',
        v_account.scopes,v_account.environment,p_key_prefix,v_key_expiry,
        p_actor_user_id
    ) returning id into v_key_id;
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        'service_key.rotated','service_account',p_service_account_id::text,
        'committed');
    return jsonb_build_object(
        'ok',true,'key_id',v_key_id,'prefix',p_key_prefix,
        'expires_at',v_key_expiry);
end;
$$;
revoke all on function public.company_admin_rotate_service_key(
    uuid,uuid,uuid,text,text,timestamptz,text
) from public, anon, authenticated;
grant execute on function public.company_admin_rotate_service_key(
    uuid,uuid,uuid,text,text,timestamptz,text
) to service_role;

-- The return contract now carries the owner resolved from organizations, not
-- the mutable credential row. Dropping is required because PostgreSQL cannot
-- change a function's table return type with CREATE OR REPLACE.
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
    account_expires_at timestamptz
)
language sql
security definer
set search_path = public, pg_temp
as $$
    select credential.key_hash,organization.billing_owner_id::text,
           credential.organization_id,credential.service_account_id,
           credential.key_type,credential.scopes,credential.environment,
           credential.expires_at,account.expires_at
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

commit;
