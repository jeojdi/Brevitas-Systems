\set ON_ERROR_STOP on

begin;

insert into auth.users(id,email) values
    ('cf200000-0000-4000-8000-000000000001',
     'billing-race-old-owner@example.invalid'),
    ('cf200000-0000-4000-8000-000000000002',
     'billing-race-new-owner@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values (
    'cf300000-0000-4000-8000-000000000001',
    'Billing owner transfer race fixture',
    'cf200000-0000-4000-8000-000000000001'
) on conflict (id) do update set
    billing_owner_id=excluded.billing_owner_id;

insert into public.organization_members(
    organization_id,user_id,role,status,disabled_at,removed_at
) values
    ('cf300000-0000-4000-8000-000000000001',
     'cf200000-0000-4000-8000-000000000001','company_owner','active',null,null),
    ('cf300000-0000-4000-8000-000000000001',
     'cf200000-0000-4000-8000-000000000002','company_owner','active',null,null)
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status,
    disabled_at=null,removed_at=null,updated_at=pg_catalog.clock_timestamp();

delete from public.billing_checkout_reservations
 where organization_id='cf300000-0000-4000-8000-000000000001';
delete from public.billing_accounts
 where organization_id='cf300000-0000-4000-8000-000000000001';

-- Model the empty account snapshot observed immediately before Stripe create.
insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status
) values (
    'cf300000-0000-4000-8000-000000000001',
    'cf200000-0000-4000-8000-000000000001',
    null,
    'not_started'
);

commit;
