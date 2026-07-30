\set ON_ERROR_STOP on

-- Run after 202607200007 and migration-company-billing-assertions.sql. These
-- checks exercise cross-company denial, canonical roles, safe retry semantics,
-- and append-only evidence against real PostgreSQL functions and triggers.

-- 202607280006 retired queue_brevitas_fee_after_usage. The two ledger rows this
-- file resolves are normally already seeded by
-- migration-company-billing-assertions.sql (which runs immediately before it);
-- this idempotent re-seed keeps the `into strict` lookups below from degrading
-- into no_data_found if that ordering ever changes. The predicate and the
-- 25%-of-verified floor arithmetic reproduce the retired queue_brevitas_fee()
-- (202607200006:192-230); billing_ledger has no BEFORE INSERT trigger
-- (202607170004:471-509), only delete/identity guards.
insert into public.billing_ledger(
    usage_log_id,organization_id,user_id,occurred_at,fee_microusd
)
select usage.id,
       usage.organization_id,
       organization.billing_owner_id,
       usage.ts,
       floor(
         least(
           greatest(coalesce(usage.brevitas_fee_usd,0),0),
           greatest(coalesce(usage.verified_savings_usd,0),0)*0.25
         )*1000000
       )::bigint
  from public.usage_log usage
  join public.billing_accounts account
    on account.organization_id=usage.organization_id
  join public.organizations organization
    on organization.id=account.organization_id
 where usage.request_id in ('company-billing-usage-a','company-billing-usage-b')
   and usage.authoritative
   and usage.organization_id is not null
   and usage.owner_id<>''
   and usage.pricing_status='priced'
   and account.subscription_status in ('active','trialing')
   and account.billing_started_at is not null
   and usage.ts>=account.billing_started_at
   and organization.billing_owner_id is not null
on conflict (usage_log_id) do nothing;

do $$
declare
    v_ledger_a bigint;
    v_ledger_b bigint;
    v_result jsonb;
begin
    select ledger.id into strict v_ledger_a
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.request_id='company-billing-usage-a';
    select ledger.id into strict v_ledger_b
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id=ledger.usage_log_id
     where usage.request_id='company-billing-usage-b';

    update public.billing_ledger
       set status='review',attempts=2,max_attempts=5,
           outbound_started_at=now()-interval '5 minutes'
     where id in (v_ledger_a,v_ledger_b);

    -- The owner's active company is fixture B. A valid fixture-A ledger id is
    -- therefore indistinguishable from a missing id and cannot be changed.
    v_result:=public.manually_resolve_billing_ledger_entry(
        'cb000000-0000-4000-8000-000000000001',
        'cb200000-0000-4000-8000-000000000002',
        v_ledger_a,'dead','Cross-company denial fixture note',
        'billing-recovery-cross-denied'
    );
    if v_result->>'code'<>'ineligible'
       or (select status from public.billing_ledger where id=v_ledger_a)<>'review' then
        raise exception 'manual recovery crossed the active company boundary';
    end if;

    -- An explicit safe retry clears only the ambiguous outbound marker and
    -- preserves bounded attempt accounting for the continuous worker.
    v_result:=public.manually_resolve_billing_ledger_entry(
        'cb000000-0000-4000-8000-000000000001',
        'cb200000-0000-4000-8000-000000000002',
        v_ledger_b,'pending','Stripe confirms fixture B was not accepted',
        'billing-recovery-safe-retry'
    );
    if coalesce((v_result->>'ok')::boolean,false) is not true
       or (select status from public.billing_ledger where id=v_ledger_b)<>'pending'
       or (select outbound_started_at from public.billing_ledger where id=v_ledger_b) is not null
       or (select max_attempts from public.billing_ledger where id=v_ledger_b)<3 then
        raise exception 'manual recovery did not preserve safe retry semantics';
    end if;

    -- The dedicated billing admin is active in fixture A and may resolve its
    -- row, while the ordinary company admin remains denied by billing:manage.
    v_result:=public.manually_resolve_billing_ledger_entry(
        'cb000000-0000-4000-8000-000000000002',
        'cb100000-0000-4000-8000-000000000001',
        v_ledger_a,'reported','Stripe aggregate confirms fixture A acceptance',
        'billing-recovery-admin-commit'
    );
    if coalesce((v_result->>'ok')::boolean,false) is not true
       or (select status from public.billing_ledger where id=v_ledger_a)<>'reported' then
        raise exception 'billing admin could not resolve its active company row';
    end if;

    v_result:=public.manually_resolve_billing_ledger_entry(
        'cb000000-0000-4000-8000-000000000003',
        'cb100000-0000-4000-8000-000000000001',
        v_ledger_a,'dead','Company admin must remain billing denied',
        'billing-recovery-role-denied'
    );
    if v_result->>'code'<>'forbidden'
       or (select status from public.billing_ledger where id=v_ledger_a)<>'reported' then
        raise exception 'company admin gained manual billing recovery permission';
    end if;

    if not exists (
        select 1 from public.billing_recovery_audit audit
         where audit.organization_id='cb200000-0000-4000-8000-000000000002'
           and audit.actor_id='cb000000-0000-4000-8000-000000000001'
           and audit.actor_role='company_owner'
           and audit.request_id='billing-recovery-safe-retry'
           and audit.ledger_entry_id=v_ledger_b
           and audit.note='Stripe confirms fixture B was not accepted'
           and audit.outcome='committed'
           and audit.result_code='resolved'
    ) or not exists (
        select 1 from public.billing_recovery_audit audit
         where audit.organization_id='cb100000-0000-4000-8000-000000000001'
           and audit.actor_id='cb000000-0000-4000-8000-000000000003'
           and audit.actor_role='company_admin'
           and audit.request_id='billing-recovery-role-denied'
           and audit.outcome='denied'
           and audit.result_code='forbidden'
    ) then
        raise exception 'manual recovery audit evidence is incomplete';
    end if;

    begin
        update public.billing_recovery_audit
           set note='Attempted evidence rewrite is forbidden'
         where request_id='billing-recovery-safe-retry';
        raise exception 'billing recovery audit update was allowed';
    exception when sqlstate '55000' then
        null;
    end;
    begin
        delete from public.billing_recovery_audit
         where request_id='billing-recovery-safe-retry';
        raise exception 'billing recovery audit delete was allowed';
    exception when sqlstate '55000' then
        null;
    end;
end;
$$;

do $$
begin
    if to_regprocedure(
        'public.manually_resolve_billing_ledger_entry(bigint,text,text)'
    ) is not null then
        raise exception 'legacy unscoped manual recovery RPC still exists';
    end if;
    if has_function_privilege(
        'authenticated',
        'public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text)',
        'execute'
    ) or has_function_privilege(
        'anon',
        'public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text)',
        'execute'
    ) or not has_function_privilege(
        'service_role',
        'public.manually_resolve_billing_ledger_entry(uuid,uuid,bigint,text,text,text)',
        'execute'
    ) then
        raise exception 'scoped manual recovery RPC grants are unsafe';
    end if;
end;
$$;
