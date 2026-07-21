-- Tenant-scoped, two-factor manual billing recovery with immutable evidence.
--
-- The HTTP recovery secret is only a second factor. The database independently
-- resolves the authenticated human's active company and canonical billing
-- permission before it can change a ledger row.

begin;

create table if not exists public.billing_recovery_audit (
    id bigint generated always as identity primary key,
    organization_id uuid not null
        references public.organizations(id) on delete restrict,
    actor_id text not null,
    actor_role text not null check (actor_role in (
        'company_owner','company_admin','member','billing_admin','none'
    )),
    request_id text not null check (
        request_id ~ '^[A-Za-z0-9._:-]{8,128}$'
    ),
    ledger_entry_id bigint not null check (ledger_entry_id > 0),
    requested_resolution text not null check (
        requested_resolution in ('reported','dead','pending')
    ),
    prior_status text,
    outcome text not null check (outcome in ('committed','denied')),
    result_code text not null check (
        result_code in (
            'resolved','forbidden','active_company_changed','ineligible'
        )
    ),
    note text not null check (
        note=btrim(note) and char_length(note) between 12 and 480
    ),
    occurred_at timestamptz not null default clock_timestamp()
);
create index if not exists billing_recovery_audit_company_time_idx
    on public.billing_recovery_audit(organization_id,occurred_at desc,id desc);
create index if not exists billing_recovery_audit_request_idx
    on public.billing_recovery_audit(request_id);
alter table public.billing_recovery_audit enable row level security;
revoke all on table public.billing_recovery_audit
    from public, anon, authenticated, service_role;

-- Service-role requests cannot rewrite, remove, or truncate recovery evidence.
-- A database owner must deliberately disable this trigger to alter the record.
create or replace function public.reject_billing_recovery_audit_mutation()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    raise exception 'billing_recovery_audit is append-only'
        using errcode='55000';
end;
$$;
revoke all on function public.reject_billing_recovery_audit_mutation()
    from public, anon, authenticated, service_role;
drop trigger if exists billing_recovery_audit_reject_update_delete
    on public.billing_recovery_audit;
create trigger billing_recovery_audit_reject_update_delete
    before update or delete on public.billing_recovery_audit
    for each row execute function public.reject_billing_recovery_audit_mutation();
drop trigger if exists billing_recovery_audit_reject_truncate
    on public.billing_recovery_audit;
create trigger billing_recovery_audit_reject_truncate
    before truncate on public.billing_recovery_audit
    for each statement execute function public.reject_billing_recovery_audit_mutation();

-- Remove the global three-argument mutation surface. The replacement accepts
-- an authenticated actor and the company previously resolved by the server,
-- but re-resolves both company and role inside this transaction. It never
-- accepts a caller-supplied role.
do $$
begin
    if to_regprocedure(
        'public.manually_resolve_billing_ledger_entry(bigint,text,text)'
    ) is not null then
        revoke all on function public.manually_resolve_billing_ledger_entry(
            bigint,text,text
        ) from public, anon, authenticated, service_role;
    end if;
end;
$$;
drop function if exists public.manually_resolve_billing_ledger_entry(
    bigint,text,text
);

create or replace function public.manually_resolve_billing_ledger_entry(
    p_actor_user_id uuid,
    p_expected_organization_id uuid,
    p_entry_id bigint,
    p_resolution text,
    p_note text,
    p_request_id text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_selection jsonb;
    v_organization_id uuid;
    v_role text;
    v_prior_status text;
    v_audit_id bigint;
begin
    if p_actor_user_id is null
       or p_expected_organization_id is null
       or p_entry_id is null or p_entry_id<=0
       or p_resolution not in ('reported','dead','pending')
       or p_note is null or p_note<>btrim(p_note)
       or char_length(p_note) not between 12 and 480
       or p_request_id is null
       or p_request_id !~ '^[A-Za-z0-9._:-]{8,128}$' then
        raise exception 'invalid scoped manual billing resolution'
            using errcode='22023';
    end if;

    v_selection:=public.company_admin_resolve_active_membership(
        p_actor_user_id
    );
    if coalesce((v_selection->>'ok')::boolean,false) is not true then
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    v_organization_id:=(v_selection->>'company_id')::uuid;

    -- Re-read and lock the active membership so disable/removal/role-change
    -- cannot race this mutation. Permission comes only from the canonical map.
    select member.role into v_role
      from public.organization_members member
     where member.organization_id=v_organization_id
       and member.user_id=p_actor_user_id
       and member.status='active'
     for update;

    if v_role is null
       or not ('billing:manage'=any(public.company_role_permissions(v_role))) then
        insert into public.billing_recovery_audit(
            organization_id,actor_id,actor_role,request_id,ledger_entry_id,
            requested_resolution,prior_status,outcome,result_code,note
        ) values (
            v_organization_id,p_actor_user_id::text,coalesce(v_role,'none'),
            p_request_id,p_entry_id,p_resolution,null,'denied','forbidden',p_note
        ) returning id into v_audit_id;
        return jsonb_build_object(
            'ok',false,'code','forbidden','audit_id',v_audit_id
        );
    end if;

    if v_organization_id<>p_expected_organization_id then
        insert into public.billing_recovery_audit(
            organization_id,actor_id,actor_role,request_id,ledger_entry_id,
            requested_resolution,prior_status,outcome,result_code,note
        ) values (
            v_organization_id,p_actor_user_id::text,v_role,p_request_id,
            p_entry_id,p_resolution,null,'denied','active_company_changed',p_note
        ) returning id into v_audit_id;
        return jsonb_build_object(
            'ok',false,'code','active_company_changed','audit_id',v_audit_id
        );
    end if;

    -- The organization predicate is mandatory. A valid ledger id belonging to
    -- another company is deliberately indistinguishable from a missing id.
    select ledger.status into v_prior_status
      from public.billing_ledger ledger
     where ledger.id=p_entry_id
       and ledger.organization_id=v_organization_id
     for update;

    if v_prior_status is null
       or v_prior_status not in ('review','dead')
       or (p_resolution='pending' and exists (
            select 1 from public.billing_ledger ledger
             where ledger.id=p_entry_id
               and ledger.organization_id=v_organization_id
               and ledger.attempts>=10
       )) then
        insert into public.billing_recovery_audit(
            organization_id,actor_id,actor_role,request_id,ledger_entry_id,
            requested_resolution,prior_status,outcome,result_code,note
        ) values (
            v_organization_id,p_actor_user_id::text,v_role,p_request_id,
            p_entry_id,p_resolution,v_prior_status,'denied','ineligible',p_note
        ) returning id into v_audit_id;
        return jsonb_build_object(
            'ok',false,'code','ineligible','audit_id',v_audit_id
        );
    end if;

    update public.billing_ledger
       set status=p_resolution,
           reported_at=case
               when p_resolution='reported' then clock_timestamp()
               else reported_at
           end,
           last_error=left('manual recovery: ' || p_note,500),
           lease_owner=null,
           lease_expires_at=null,
           next_attempt_at=case
               when p_resolution='pending' then clock_timestamp()
               else next_attempt_at
           end,
           max_attempts=case
               when p_resolution='pending'
                   then least(10,greatest(max_attempts,attempts+1))
               else max_attempts
           end,
           -- A retry attests that Stripe did not accept the stable event id.
           outbound_started_at=case
               when p_resolution='pending' then null
               else outbound_started_at
           end
     where id=p_entry_id
       and organization_id=v_organization_id;

    insert into public.billing_recovery_audit(
        organization_id,actor_id,actor_role,request_id,ledger_entry_id,
        requested_resolution,prior_status,outcome,result_code,note
    ) values (
        v_organization_id,p_actor_user_id::text,v_role,p_request_id,
        p_entry_id,p_resolution,v_prior_status,'committed','resolved',p_note
    ) returning id into v_audit_id;

    return jsonb_build_object(
        'ok',true,'code','resolved','audit_id',v_audit_id,
        'organization_id',v_organization_id,'actor_role',v_role,
        'prior_status',v_prior_status,'resolution',p_resolution
    );
end;
$$;
revoke all on function public.manually_resolve_billing_ledger_entry(
    uuid,uuid,bigint,text,text,text
) from public, anon, authenticated;
grant execute on function public.manually_resolve_billing_ledger_entry(
    uuid,uuid,bigint,text,text,text
) to service_role;

comment on table public.billing_recovery_audit is
    'Append-only tenant evidence for human manual billing-ledger recovery. Notes must exclude secrets and customer content.';

-- ROLLBACK (maintenance window; preserve evidence before any object removal):
-- 1. REVOKE EXECUTE ON FUNCTION public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text) FROM service_role;
-- 2. Do not restore the unscoped three-argument recovery RPC.
-- 3. Keep billing_recovery_audit and its append-only triggers for seven-year financial evidence retention.

commit;
