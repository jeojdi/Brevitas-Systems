\set ON_ERROR_STOP on

-- Migration 009 keeps up to eight concurrent browser tabs for one actor and
-- rotates only that actor's oldest session. Another member's session in the
-- same company must remain untouched.

insert into auth.users(id,email) values
    ('59000000-0000-4000-8000-000000000001','multitab-owner@example.invalid'),
    ('59000000-0000-4000-8000-000000000002','multitab-member@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,legacy_owner_id,billing_owner_id) values (
    '59100000-0000-4000-8000-000000000001','Multi-tab session fixture',
    'multitab-session-fixture','59000000-0000-4000-8000-000000000001'
) on conflict (id) do nothing;

insert into public.organization_members(
    organization_id,user_id,role,status
) values
    ('59100000-0000-4000-8000-000000000001','59000000-0000-4000-8000-000000000001','company_owner','active'),
    ('59100000-0000-4000-8000-000000000001','59000000-0000-4000-8000-000000000002','member','active')
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status;

do $$
declare
    v_created jsonb;
    v_owner_key_ids uuid[]:=array[]::uuid[];
    v_member_key_id uuid;
    v_new_key_id uuid;
    v_hash text;
begin
    for v_index in 1..8 loop
        v_hash:=encode(digest(
            'release-multitab-owner-'||v_index::text,'sha256'
        ),'hex');
        v_created:=public.company_admin_create_dashboard_session_key(
            '59100000-0000-4000-8000-000000000001',
            '59000000-0000-4000-8000-000000000001',
            v_hash,format('bvt_tab%s',lpad(v_index::text,2,'0')),
            now()+interval '1 hour',
            format('release-multitab-owner-%s',lpad(v_index::text,2,'0'))
        );
        if coalesce((v_created->>'ok')::boolean,false) is not true
           or (v_created->>'rotated_count')::integer<>0 then
            raise exception 'dashboard session rotated before the actor cap';
        end if;
        v_owner_key_ids:=array_append(
            v_owner_key_ids,(v_created->>'key_id')::uuid
        );
    end loop;

    -- now() is transaction-stable, so make the first fixture unambiguously
    -- oldest before applying cap pressure.
    update public.api_keys set created=now()-interval '1 hour'
     where id=v_owner_key_ids[1];

    v_created:=public.company_admin_create_dashboard_session_key(
        '59100000-0000-4000-8000-000000000001',
        '59000000-0000-4000-8000-000000000002',
        encode(digest('release-multitab-member','sha256'),'hex'),
        'bvt_membertab',now()+interval '1 hour','release-multitab-member'
    );
    v_member_key_id:=(v_created->>'key_id')::uuid;
    if coalesce((v_created->>'ok')::boolean,false) is not true then
        raise exception 'second actor could not create an independent tab';
    end if;

    v_created:=public.company_admin_create_dashboard_session_key(
        '59100000-0000-4000-8000-000000000001',
        '59000000-0000-4000-8000-000000000001',
        encode(digest('release-multitab-owner-9','sha256'),'hex'),
        'bvt_tab09',now()+interval '1 hour','release-multitab-owner-09'
    );
    v_new_key_id:=(v_created->>'key_id')::uuid;
    if coalesce((v_created->>'ok')::boolean,false) is not true
       or (v_created->>'rotated_count')::integer<>1
       or (select revoked_at from public.api_keys
            where id=v_owner_key_ids[1]) is null
       or (select revoked_at from public.api_keys
            where id=v_member_key_id) is not null
       or (select count(*) from public.api_keys
            where organization_id='59100000-0000-4000-8000-000000000001'
              and key_type='dashboard_session'
              and created_by='59000000-0000-4000-8000-000000000001'
              and revoked_at is null)<>8 then
        raise exception 'actor-scoped dashboard session cap is incorrect';
    end if;

    update public.api_keys set expires_at=now()-interval '1 second'
     where id=v_owner_key_ids[2];
    v_created:=public.company_admin_create_dashboard_session_key(
        '59100000-0000-4000-8000-000000000001',
        '59000000-0000-4000-8000-000000000001',
        encode(digest('release-multitab-owner-10','sha256'),'hex'),
        'bvt_tab10',now()+interval '1 hour','release-multitab-owner-10'
    );
    if coalesce((v_created->>'ok')::boolean,false) is not true
       or (v_created->>'rotated_count')::integer<>0
       or (select revoked_at from public.api_keys
            where id=v_owner_key_ids[2]) is null
       or (select revoked_at from public.api_keys
            where id=v_member_key_id) is not null
       or not exists (
            select 1 from public.audit_events
             where organization_id='59100000-0000-4000-8000-000000000001'
               and request_id='release-multitab-owner-09'
               and action='dashboard_session.rotated'
               and target_id=v_owner_key_ids[1]::text
               and outcome='committed'
       ) then
        raise exception 'expired-session cleanup or rotation evidence is incorrect';
    end if;
end;
$$;
