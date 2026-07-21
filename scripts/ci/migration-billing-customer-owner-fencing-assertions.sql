\set ON_ERROR_STOP on

-- The RPC must resolve ownership at persistence time. These checks model an
-- authorization snapshot taken before an owner transfer and prove it cannot be
-- supplied to, or reintroduced by, the persistence contract.
begin;

insert into auth.users(id,email) values
    ('cf000000-0000-4000-8000-000000000001',
     'billing-customer-old-owner@example.invalid'),
    ('cf000000-0000-4000-8000-000000000002',
     'billing-customer-new-owner@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values (
    'cf100000-0000-4000-8000-000000000001',
    'Billing customer owner-fencing fixture',
    'cf000000-0000-4000-8000-000000000001'
) on conflict (id) do update set
    billing_owner_id=excluded.billing_owner_id;

insert into public.organization_members(
    organization_id,user_id,role,status
) values
    ('cf100000-0000-4000-8000-000000000001',
     'cf000000-0000-4000-8000-000000000001','company_owner','active'),
    ('cf100000-0000-4000-8000-000000000001',
     'cf000000-0000-4000-8000-000000000002','company_owner','active')
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status;

select public.save_billing_customer_identity(
    'cf100000-0000-4000-8000-000000000001',
    'cus_owner_fencing_fixture'
);

-- This is the ownership change that can complete while Stripe customer create
-- is in flight. The later RPC has no stale user-id argument and must use owner 2.
update public.organizations
   set billing_owner_id='cf000000-0000-4000-8000-000000000002'
 where id='cf100000-0000-4000-8000-000000000001';

select public.save_billing_customer_identity(
    'cf100000-0000-4000-8000-000000000001',
    'cus_owner_fencing_fixture'
);

do $owner_fencing_assertions$
declare
    v_ledger_count bigint;
begin
    if not exists (
        select 1
          from public.billing_accounts
         where organization_id='cf100000-0000-4000-8000-000000000001'
           and user_id='cf000000-0000-4000-8000-000000000002'
           and stripe_customer_id='cus_owner_fencing_fixture'
    ) then
        raise exception 'stale Checkout attribution overwrote the current billing owner';
    end if;

    select count(*) into v_ledger_count
      from public.billing_ledger
     where organization_id='cf100000-0000-4000-8000-000000000001';
    if v_ledger_count <> 0 then
        raise exception 'customer identity persistence changed organization ledger state';
    end if;

    if has_function_privilege(
           'anon',
           'public.save_billing_customer_identity(uuid,text)',
           'EXECUTE')
       or has_function_privilege(
           'authenticated',
           'public.save_billing_customer_identity(uuid,text)',
           'EXECUTE')
       or not has_function_privilege(
           'service_role',
           'public.save_billing_customer_identity(uuid,text)',
           'EXECUTE') then
        raise exception 'billing customer persistence privilege boundary is invalid';
    end if;
end;
$owner_fencing_assertions$;

-- Existing customer identity is immutable even if a stale read observed NULL.
do $customer_conflict_assertion$
begin
    begin
        perform public.save_billing_customer_identity(
            'cf100000-0000-4000-8000-000000000001',
            'cus_stale_checkout_must_not_replace'
        );
        raise exception 'different Stripe customer identity was overwritten';
    exception
        when unique_violation then null;
    end;
end;
$customer_conflict_assertion$;

-- A disabled billing owner is not valid attribution. Failure must leave the
-- previously persisted company account unchanged.
update public.organization_members
   set status='disabled',updated_at=pg_catalog.clock_timestamp(),
       disabled_at=pg_catalog.clock_timestamp()
 where organization_id='cf100000-0000-4000-8000-000000000001'
   and user_id='cf000000-0000-4000-8000-000000000002';

do $inactive_owner_assertion$
begin
    begin
        perform public.save_billing_customer_identity(
            'cf100000-0000-4000-8000-000000000001',
            'cus_owner_fencing_fixture'
        );
        raise exception 'inactive billing owner was accepted for persistence';
    exception
        when check_violation then null;
    end;
end;
$inactive_owner_assertion$;

rollback;
