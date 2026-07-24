-- Bounded concurrent dashboard credentials for multi-tab browser sessions.
-- Each credential remains tenant/actor bound and expires within eight hours.
-- The company administration namespace lock serializes cleanup, cap pressure,
-- rotation, insertion, and audit evidence across every API replica.

begin;

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
    v_rotated_key_id uuid;
    v_active_count bigint;
    v_actor_active_count bigint;
    v_revoke_count integer := 0;
    v_rotated_count integer := 0;
    v_scopes text[] := array[
        'proxy:invoke','usage:read_own','provider:read','provider:manage'
    ]::text[];
begin
    perform public.lock_company_admin_namespace(p_organization_id);
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);

    if v_actor_role is null or v_actor_role not in (
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

    -- Expired credentials are unusable. Transition them before counting so
    -- abandoned tabs cannot consume either the actor cap or the company cap.
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

    -- Keep at most eight live browser sessions for this actor/company and at
    -- most 1,000 for the company. Under pressure only this actor's oldest
    -- sessions may rotate; another member's credential is never addressed.
    v_revoke_count := greatest(
        greatest(v_actor_active_count-7,0),
        greatest(v_active_count-999,0)
    )::integer;
    if v_revoke_count>v_actor_active_count then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'dashboard_session.create.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object('ok',false,'code','company_session_cap');
    end if;

    for v_rotated_key_id in
        select credential.id
          from public.api_keys credential
         where credential.organization_id=p_organization_id
           and credential.key_type='dashboard_session'
           and credential.created_by=p_actor_user_id
           and credential.revoked_at is null
           and credential.expires_at>now()
         order by credential.created,credential.id
         limit v_revoke_count
    loop
        update public.api_keys
           set revoked_at=now()
         where organization_id=p_organization_id
           and id=v_rotated_key_id
           and key_type='dashboard_session'
           and created_by=p_actor_user_id
           and revoked_at is null;
        v_rotated_count := v_rotated_count+1;
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'dashboard_session.rotated','api_key',
            v_rotated_key_id::text,'committed');
    end loop;

    insert into public.api_keys(
        key_hash,name,created,owner_id,organization_id,service_account_id,
        key_type,scopes,environment,key_prefix,expires_at,created_by
    ) values (
        p_key_hash,'dashboard session',now(),p_actor_user_id::text,
        p_organization_id,null,'dashboard_session',v_scopes,'dashboard',
        p_key_prefix,p_expires_at,p_actor_user_id
    ) returning id into v_key_id;

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
        'expires_at',p_expires_at,
        'rotated_count',v_rotated_count
    );
exception when unique_violation then
    -- The exception block rolls back cap rotation before recording denial, so
    -- a digest collision cannot strand an otherwise valid browser session.
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

commit;
