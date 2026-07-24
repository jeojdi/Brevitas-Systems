-- Server-owned active-company selection for multi-organization dashboard users.
-- The browser may request a target UUID, but only service-role RPCs can read or
-- change this preference and every change re-authorizes the verified actor.

create table if not exists public.active_company_selections (
    user_id uuid primary key references auth.users(id) on delete cascade,
    organization_id uuid not null,
    updated_at timestamptz not null default now(),
    foreign key (organization_id, user_id)
        references public.organization_members(organization_id, user_id)
        on delete cascade
);
create index if not exists active_company_selections_organization_idx
    on public.active_company_selections(organization_id, user_id);
alter table public.active_company_selections enable row level security;
revoke all on table public.active_company_selections
    from public, anon, authenticated, service_role;

-- Resolve the saved choice against current membership state. A missing or stale
-- preference falls back deterministically to another live membership and repairs
-- the preference in the same transaction.
create or replace function public.company_admin_resolve_active_membership(
    p_actor_user_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_organization_id uuid;
    v_role text;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_actor_user_id::text, 0));

    select selection.organization_id
      into v_organization_id
      from public.active_company_selections selection
     where selection.user_id = p_actor_user_id;

    if v_organization_id is not null then
        select member.role
          into v_role
          from public.organization_members member
         where member.organization_id = v_organization_id
           and member.user_id = p_actor_user_id
           and member.status = 'active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin'
           )
         for share;
    end if;

    if v_role is null then
        select member.organization_id, member.role
          into v_organization_id, v_role
          from public.organization_members member
         where member.user_id = p_actor_user_id
           and member.status = 'active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin'
           )
         order by member.created_at, member.organization_id
         limit 1
         for share;
    end if;

    if v_role is null then
        delete from public.active_company_selections
         where user_id = p_actor_user_id;
        return jsonb_build_object('ok', false, 'code', 'no_active_membership');
    end if;

    insert into public.active_company_selections(
        user_id, organization_id, updated_at
    ) values (
        p_actor_user_id, v_organization_id, now()
    )
    on conflict (user_id) do update set
        organization_id = excluded.organization_id,
        updated_at = excluded.updated_at;

    return jsonb_build_object(
        'ok', true,
        'company_id', v_organization_id,
        'role', v_role
    );
end;
$$;
revoke all on function public.company_admin_resolve_active_membership(uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.company_admin_resolve_active_membership(uuid)
    to service_role;

-- Persist an explicit choice only if the verified actor is currently active in
-- the requested organization. The membership lock serializes concurrent removal
-- or disable operations with the selection.
create or replace function public.company_admin_select_active_membership(
    p_actor_user_id uuid,
    p_requested_organization_id uuid,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_role text;
begin
    if p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$' then
        return jsonb_build_object('ok', false, 'code', 'invalid_request');
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_actor_user_id::text, 0));
    select member.role
      into v_role
      from public.organization_members member
     where member.organization_id = p_requested_organization_id
       and member.user_id = p_actor_user_id
       and member.status = 'active'
       and member.role in (
           'company_owner','company_admin','member','billing_admin'
       )
     for update;

    if v_role is null then
        return jsonb_build_object('ok', false, 'code', 'forbidden');
    end if;

    insert into public.active_company_selections(
        user_id, organization_id, updated_at
    ) values (
        p_actor_user_id, p_requested_organization_id, now()
    )
    on conflict (user_id) do update set
        organization_id = excluded.organization_id,
        updated_at = excluded.updated_at;

    perform public.append_company_audit(
        p_requested_organization_id,
        p_actor_user_id::text,
        v_role,
        p_request_id,
        'company.active_selected',
        'company',
        p_requested_organization_id::text,
        'committed'
    );

    return jsonb_build_object(
        'ok', true,
        'company_id', p_requested_organization_id,
        'role', v_role
    );
end;
$$;
revoke all on function public.company_admin_select_active_membership(uuid,uuid,text)
    from public, anon, authenticated, service_role;
grant execute on function public.company_admin_select_active_membership(uuid,uuid,text)
    to service_role;
