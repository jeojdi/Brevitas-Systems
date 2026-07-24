-- Bounded active-company choices for authenticated administration/device UI.
-- Requires migrations through 202607170010. The API supplies only the actor ID
-- from its verified Supabase identity and the already-derived active company.

create index if not exists organization_members_actor_active_idx
    on public.organization_members(user_id, organization_id)
    where status='active';

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
    -- Reject a stale/foreign active-company context before listing anything.
    v_active_role := public.lock_company_actor_role(
        p_active_organization_id,p_actor_user_id);
    if v_active_role is null or v_active_role not in (
        'company_owner','company_admin','member','billing_admin'
    ) then
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;

    -- Lock the selected membership rows through serialization. Current first,
    -- then a deterministic content-safe company name/UUID order, hard-capped.
    select coalesce(
        jsonb_agg(
            jsonb_build_object(
                'company_id',page.company_id,
                'company_name',page.company_name,
                'role',page.role
            ) order by page.is_current desc,page.company_name,page.company_id
        ),
        '[]'::jsonb
    ) into v_items
      from (
        select member.organization_id as company_id,
               left(organization.name,200) as company_name,
               member.role,
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
