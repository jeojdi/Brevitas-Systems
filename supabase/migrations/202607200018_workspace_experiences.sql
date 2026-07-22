-- Persist the product experience selected during first-run onboarding. This is
-- presentation metadata on the tenant itself; authorization continues to come
-- exclusively from the actor's locked organization membership.

begin;

alter table public.organizations
    add column if not exists account_type text not null default 'company';

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname = 'organizations_account_type_check'
           and conrelid = 'public.organizations'::regclass
    ) then
        alter table public.organizations
            add constraint organizations_account_type_check
            check (account_type in ('individual','company'));
    end if;
end;
$$;

create or replace function public.ensure_workspace_organization(
    p_user_id uuid,
    p_name text,
    p_account_type text
) returns table(id uuid, name text, role text, billing_owner_id uuid, account_type text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_organization public.organizations%rowtype;
begin
    if p_account_type not in ('individual','company') then
        raise exception 'invalid account type';
    end if;

    insert into public.organizations(
        name, legacy_owner_id, billing_owner_id, account_type
    ) values (
        coalesce(nullif(btrim(p_name), ''), 'My organization'),
        p_user_id::text,
        p_user_id,
        p_account_type
    )
    on conflict (legacy_owner_id) do update
        set legacy_owner_id = excluded.legacy_owner_id
    returning * into v_organization;

    insert into public.organization_members(
        organization_id,user_id,role,status
    ) values (
        v_organization.id,p_user_id,'company_owner','active'
    )
    on conflict (organization_id,user_id) do nothing;

    return query
    select v_organization.id,
           v_organization.name,
           member.role,
           v_organization.billing_owner_id,
           v_organization.account_type
      from public.organization_members member
     where member.organization_id=v_organization.id
       and member.user_id=p_user_id;
end;
$$;

revoke all on function public.ensure_workspace_organization(uuid,text,text)
    from public, anon, authenticated, service_role;
grant execute on function public.ensure_workspace_organization(uuid,text,text)
    to service_role;

-- Return the persisted experience with every server-derived workspace choice so
-- a browser cannot use a route or a query parameter to unlock enterprise UI.
create or replace function public.company_admin_active_memberships(
    p_actor_user_id uuid,
    p_active_organization_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_active_role text;
    v_items jsonb;
begin
    v_active_role := public.lock_company_actor_role(
        p_active_organization_id,p_actor_user_id);
    if v_active_role is null or v_active_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) then
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;

    select coalesce(
        jsonb_agg(
            jsonb_build_object(
                'company_id',page.company_id,
                'company_name',page.company_name,
                'role',page.role,
                'account_type',page.account_type
            ) order by page.is_current desc,page.company_name,page.company_id
        ),
        '[]'::jsonb
    ) into v_items
      from (
        select member.organization_id as company_id,
               left(organization.name,200) as company_name,
               member.role,
               organization.account_type,
               member.organization_id=p_active_organization_id as is_current
          from public.organization_members member
          join public.organizations organization
            on organization.id=member.organization_id
         where member.user_id=p_actor_user_id
           and member.status='active'
           and member.role in (
               'company_owner','company_admin','member','billing_admin'
           )
         order by is_current desc,company_name,company_id
         limit 100
         for share of member
      ) page;

    return jsonb_build_object('ok',true,'items',v_items);
end;
$$;

revoke all on function public.company_admin_active_memberships(uuid,uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.company_admin_active_memberships(uuid,uuid)
    to service_role;

commit;
