-- Transactional authorization boundary for the legacy /v1/keys dashboard API.
-- Requires migrations through 202607170008. Key digests and raw credentials
-- never leave api_keys; callers receive bounded metadata only.

create index if not exists api_keys_tenant_page_idx
    on public.api_keys(organization_id, created desc, id desc);
create index if not exists api_keys_actor_dashboard_page_idx
    on public.api_keys(organization_id, created_by, created desc, id desc)
    where key_type='dashboard_session';

create or replace function public.company_admin_dashboard_keys_page(
    p_organization_id uuid,
    p_actor_user_id uuid,
    p_cursor_time timestamptz,
    p_cursor_id uuid,
    p_limit integer,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_actor_role text;
    v_items jsonb;
begin
    -- The membership row remains locked through authorization and selection,
    -- so a concurrent disable/remove cannot race a service-role table read.
    v_actor_role := public.lock_company_actor_role(
        p_organization_id,p_actor_user_id);

    if v_actor_role is null or v_actor_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) or ((p_cursor_time is null) <> (p_cursor_id is null)) then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'dashboard_keys.read.denied','company',
            p_organization_id::text,'denied');
        return jsonb_build_object(
            'ok',false,
            'code',case
                when v_actor_role is null then 'forbidden'
                else 'invalid_cursor'
            end
        );
    end if;

    select coalesce(
        jsonb_agg(to_jsonb(page) order by page.created desc,page.id desc),
        '[]'::jsonb
    ) into v_items
      from (
        select credential.id,
               credential.name,
               credential.created,
               credential.key_type,
               credential.scopes,
               credential.environment,
               credential.key_prefix as prefix,
               case when v_actor_role in ('company_owner','company_admin')
                    then credential.service_account_id
                    else null
                end as service_account_id,
               credential.expires_at,
               credential.last_used_at,
               credential.revoked_at
          from public.api_keys credential
         where credential.organization_id=p_organization_id
           and (
                v_actor_role in ('company_owner','company_admin')
                or (
                    v_actor_role in ('member','billing_admin')
                    and credential.key_type='dashboard_session'
                    and credential.created_by=p_actor_user_id
                )
           )
           and (
                p_cursor_time is null
                or (credential.created,credential.id)<(p_cursor_time,p_cursor_id)
           )
         order by credential.created desc,credential.id desc
         limit least(greatest(coalesce(p_limit,50),1),100)+1
      ) page;

    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;
revoke all on function public.company_admin_dashboard_keys_page(
    uuid,uuid,timestamptz,uuid,integer,text
) from public, anon, authenticated, service_role;
grant execute on function public.company_admin_dashboard_keys_page(
    uuid,uuid,timestamptz,uuid,integer,text
) to service_role;

-- This RPC is intentionally dashboard-session-specific. Long-lived service
-- account credentials remain exclusively under the company service-account
-- lifecycle RPCs and cannot be revoked through DELETE /v1/keys/{id}.
create or replace function public.company_admin_revoke_dashboard_session_key(
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
     where organization_id=p_organization_id
       and id=p_key_id
     for update;

    if v_actor_role is null or v_actor_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) or v_key.id is null
      or v_key.key_type<>'dashboard_session'
      or (
        v_actor_role in ('member','billing_admin')
        and v_key.created_by is distinct from p_actor_user_id
      ) then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,
            coalesce(v_actor_role,'none'),p_request_id,
            'dashboard_session.revoke.denied','api_key',
            p_key_id::text,'denied');
        return jsonb_build_object(
            'ok',false,'code','forbidden_or_not_found');
    end if;

    if v_key.revoked_at is not null then
        perform public.append_company_audit(
            p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
            'dashboard_session.revoke.noop','api_key',
            p_key_id::text,'committed');
        return jsonb_build_object(
            'ok',true,'key_id',p_key_id,
            'revoked',false,'already_revoked',true);
    end if;

    update public.api_keys
       set revoked_at=now()
     where organization_id=p_organization_id
       and id=p_key_id
       and key_type='dashboard_session';
    perform public.append_company_audit(
        p_organization_id,p_actor_user_id::text,v_actor_role,p_request_id,
        'dashboard_session.revoked','api_key',p_key_id::text,'committed');
    return jsonb_build_object(
        'ok',true,'key_id',p_key_id,
        'revoked',true,'already_revoked',false);
end;
$$;
revoke all on function public.company_admin_revoke_dashboard_session_key(
    uuid,uuid,uuid,text
) from public, anon, authenticated, service_role;
grant execute on function public.company_admin_revoke_dashboard_session_key(
    uuid,uuid,uuid,text
) to service_role;

-- Migration 008's generic name allowed an owner/admin to address any tenant
-- key type. Remove that callable surface after installing the strict RPC.
revoke all on function public.company_admin_revoke_key(uuid,uuid,uuid,text)
    from public, anon, authenticated, service_role;
drop function if exists public.company_admin_revoke_key(uuid,uuid,uuid,text);
