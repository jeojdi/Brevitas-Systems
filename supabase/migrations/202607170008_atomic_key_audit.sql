-- Atomic dashboard-key lifecycle and immutable audit evidence.
-- Requires migrations through 202607170007. Raw keys never cross this boundary;
-- callers pass a SHA-256 digest and receive only opaque row/context metadata.

create or replace function public.company_admin_create_dashboard_session_key(
    p_organization_id uuid,
    p_actor_user_id uuid,
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
    v_key_id uuid;
    v_active_count bigint;
    v_actor_active_count bigint;
    v_scopes text[] := array[
        'proxy:invoke','usage:read_own','provider:read','provider:manage'
    ]::text[];
begin
    -- Shared namespace ordering matches migration 005 member/key administration.
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);

    if v_actor_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) or p_key_hash !~ '^[0-9a-f]{64}$'
      or p_key_prefix !~ '^bvt_[A-Za-z0-9_-]{4,12}$'
      or p_expires_at is null
      or p_expires_at<=now()
      or p_expires_at>now()+interval '8 hours' then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'dashboard_session.create.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden_or_invalid');
    end if;

    -- Expired keys are unusable and transitioned before cap enforcement. The
    -- organization lock serializes this count across all API replicas.
    update public.api_keys
       set revoked_at=now()
     where organization_id=p_organization_id
       and key_type='dashboard_session'
       and revoked_at is null
       and (expires_at is null or expires_at<=now());

    select count(*),count(*) filter (where created_by=p_actor_user_id)
      into v_active_count,v_actor_active_count
      from public.api_keys
     where organization_id=p_organization_id
       and key_type='dashboard_session'
       and revoked_at is null
       and expires_at>now();
    if v_active_count-v_actor_active_count>=1000 then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'dashboard_session.create.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','company_session_cap');
    end if;

    -- One active dashboard session per actor. Revocation, replacement, and
    -- audit append commit or roll back as one database transaction.
    update public.api_keys
       set revoked_at=now()
     where organization_id=p_organization_id
       and key_type='dashboard_session'
       and created_by=p_actor_user_id
       and revoked_at is null;

    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,service_account_id,
        key_type,scopes,environment,key_prefix,expires_at,created_by
    ) values (
        p_key_hash,'dashboard session',now(),p_actor_user_id::text,
        p_organization_id,null,'dashboard_session',v_scopes,'dashboard',
        p_key_prefix,p_expires_at,p_actor_user_id
    ) returning id into v_key_id;

    -- Audit targets the opaque row UUID, never the raw key, digest, or prefix.
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        'dashboard_session.created','api_key',v_key_id::text,'committed');

    return jsonb_build_object(
        'ok',true,
        'key_id',v_key_id,
        'organization_id',p_organization_id,
        'key_type','dashboard_session',
        'scopes',v_scopes,
        'environment','dashboard',
        'prefix',p_key_prefix,
        'expires_at',p_expires_at
    );
exception when unique_violation then
    -- A digest collision/retry cannot create a second row. The exception block
    -- rolls back replacement revocation before appending the denied evidence.
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,
        coalesce(v_actor_role,'none'),p_request_id,
        'dashboard_session.create.denied','company',
        p_organization_id::text,'denied');
    return jsonb_build_object('ok',false,'code','duplicate_key');
end;
$$;
revoke all on function public.company_admin_create_dashboard_session_key(
    uuid,uuid,text,text,timestamptz,text
) from public, anon, authenticated, service_role;
grant execute on function public.company_admin_create_dashboard_session_key(
    uuid,uuid,text,text,timestamptz,text
) to service_role;

create or replace function public.company_admin_revoke_key(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_key_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_actor_role text;
    v_key public.api_keys%rowtype;
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);

    select * into v_key
      from public.api_keys
     where organization_id=p_organization_id and id=p_key_id
     for update;

    if v_actor_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) or v_key.id is null or (
        v_actor_role in ('member','billing_admin') and not (
            v_key.key_type='dashboard_session'
            and v_key.created_by=p_actor_user_id
        )
    ) then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'api_key.revoke.denied','api_key',p_key_id::text,'denied');
        return jsonb_build_object('ok',false,'code','forbidden_or_not_found');
    end if;

    if v_key.revoked_at is not null then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'api_key.revoke.noop','api_key',p_key_id::text,'committed');
        return jsonb_build_object(
            'ok',true,'key_id',p_key_id,'revoked',false,'already_revoked',true);
    end if;

    update public.api_keys set revoked_at=now()
     where organization_id=p_organization_id and id=p_key_id;
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        'api_key.revoked','api_key',p_key_id::text,'committed');
    return jsonb_build_object(
        'ok',true,'key_id',p_key_id,'revoked',true,'already_revoked',false);
end;
$$;
revoke all on function public.company_admin_revoke_key(uuid,uuid,uuid,text)
    from public, anon, authenticated, service_role;
grant execute on function public.company_admin_revoke_key(uuid,uuid,uuid,text)
    to service_role;
