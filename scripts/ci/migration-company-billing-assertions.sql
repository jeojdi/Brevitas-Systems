\set ON_ERROR_STOP on

-- Dedicated post-migration checks for company-scoped Stripe billing. This file
-- is intentionally separate from the global fixture so the migration runner
-- can invoke it after 202607200006 without changing older baseline semantics.

insert into auth.users(id,email) values
    ('cb000000-0000-4000-8000-000000000001','company-billing-owner@example.invalid'),
    ('cb000000-0000-4000-8000-000000000002','company-billing-admin@example.invalid'),
    ('cb000000-0000-4000-8000-000000000003','company-admin-denied@example.invalid'),
    ('cb000000-0000-4000-8000-000000000004','company-member-denied@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('cb100000-0000-4000-8000-000000000001','Company billing fixture A',
     'cb000000-0000-4000-8000-000000000001'),
    ('cb200000-0000-4000-8000-000000000002','Company billing fixture B',
     'cb000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.organization_members(
    organization_id,user_id,role,status
) values
    ('cb100000-0000-4000-8000-000000000001','cb000000-0000-4000-8000-000000000001','company_owner','active'),
    ('cb200000-0000-4000-8000-000000000002','cb000000-0000-4000-8000-000000000001','company_owner','active'),
    ('cb100000-0000-4000-8000-000000000001','cb000000-0000-4000-8000-000000000002','billing_admin','active'),
    ('cb100000-0000-4000-8000-000000000001','cb000000-0000-4000-8000-000000000003','company_admin','active'),
    ('cb100000-0000-4000-8000-000000000001','cb000000-0000-4000-8000-000000000004','member','active')
on conflict (organization_id,user_id) do update set
    role=excluded.role,status=excluded.status;

insert into public.active_company_selections(user_id,organization_id) values
    ('cb000000-0000-4000-8000-000000000001','cb100000-0000-4000-8000-000000000001'),
    ('cb000000-0000-4000-8000-000000000002','cb100000-0000-4000-8000-000000000001'),
    ('cb000000-0000-4000-8000-000000000003','cb100000-0000-4000-8000-000000000001'),
    ('cb000000-0000-4000-8000-000000000004','cb100000-0000-4000-8000-000000000001')
on conflict (user_id) do update set organization_id=excluded.organization_id;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status,
    billing_started_at,current_period_start,current_period_end
) values
    ('cb100000-0000-4000-8000-000000000001','cb000000-0000-4000-8000-000000000001',
     'cus_company_billing_fixture_a','active',now()-interval '1 day',
     date_trunc('day',now()),date_trunc('day',now())+interval '7 days'),
    ('cb200000-0000-4000-8000-000000000002','cb000000-0000-4000-8000-000000000001',
     'cus_company_billing_fixture_b','active',now()-interval '1 day',
     date_trunc('day',now()),date_trunc('day',now())+interval '7 days')
on conflict (organization_id) do update set
    user_id=excluded.user_id,
    stripe_customer_id=excluded.stripe_customer_id,
    subscription_status=excluded.subscription_status,
    billing_started_at=excluded.billing_started_at,
    current_period_start=excluded.current_period_start,
    current_period_end=excluded.current_period_end;

do $$
declare
    v_context jsonb;
begin
    if (select count(*) from public.billing_accounts
         where user_id='cb000000-0000-4000-8000-000000000001')<>2 then
        raise exception 'one owner could not retain two independent company billing accounts';
    end if;

    v_context:=public.company_billing_authorize_actor(
        'cb000000-0000-4000-8000-000000000001');
    if v_context->>'organization_id'<>'cb100000-0000-4000-8000-000000000001'
       or coalesce((v_context->>'ok')::boolean,false) is not true then
        raise exception 'company owner was not authorized for the saved active company';
    end if;

    v_context:=public.company_billing_authorize_actor(
        'cb000000-0000-4000-8000-000000000002');
    if coalesce((v_context->>'ok')::boolean,false) is not true then
        raise exception 'billing admin was denied company billing';
    end if;

    v_context:=public.company_billing_authorize_actor(
        'cb000000-0000-4000-8000-000000000003');
    if coalesce((v_context->>'ok')::boolean,false) is true
       or v_context->>'code'<>'forbidden' then
        raise exception 'company admin gained billing permission';
    end if;

    v_context:=public.company_billing_authorize_actor(
        'cb000000-0000-4000-8000-000000000004');
    if coalesce((v_context->>'ok')::boolean,false) is true
       or v_context->>'code'<>'forbidden' then
        raise exception 'ordinary member gained billing permission';
    end if;

    update public.active_company_selections
       set organization_id='cb200000-0000-4000-8000-000000000002'
     where user_id='cb000000-0000-4000-8000-000000000001';
    v_context:=public.company_billing_authorize_actor(
        'cb000000-0000-4000-8000-000000000001');
    if v_context->>'organization_id'<>'cb200000-0000-4000-8000-000000000002' then
        raise exception 'billing authorization ignored the saved active company';
    end if;
end;
$$;

select public.compare_and_set_stripe_subscription_snapshot(
    'cb100000-0000-4000-8000-000000000001',0,100,'evt_companybillinga',
    'customer.subscription.created','sub_company_billing_fixture_a','active',
    now()-interval '1 day',date_trunc('day',now()),
    date_trunc('day',now())+interval '7 days'
);
select public.compare_and_set_stripe_subscription_snapshot(
    'cb200000-0000-4000-8000-000000000002',0,100,'evt_companybillingb',
    'customer.subscription.created','sub_company_billing_fixture_b','active',
    now()-interval '1 day',date_trunc('day',now()),
    date_trunc('day',now())+interval '7 days'
);

insert into public.api_keys(
    key_hash,name,owner_id,organization_id,key_type
) values
    ('company-billing-key-a','Company billing key A',
     'cb000000-0000-4000-8000-000000000001',
     'cb100000-0000-4000-8000-000000000001','legacy'),
    ('company-billing-key-b','Company billing key B',
     'cb000000-0000-4000-8000-000000000001',
     'cb200000-0000-4000-8000-000000000002','legacy')
on conflict (key_hash) do nothing;

insert into public.usage_log(
    key_hash,owner_id,organization_id,request_id,authoritative,
    pricing_status,verified_savings_usd,brevitas_fee_usd,receipt_source
) values
    ('company-billing-key-a','cb000000-0000-4000-8000-000000000001',
     'cb100000-0000-4000-8000-000000000001','company-billing-usage-a',true,
     'priced',4,1,'proxy'),
    ('company-billing-key-b','cb000000-0000-4000-8000-000000000001',
     'cb200000-0000-4000-8000-000000000002','company-billing-usage-b',true,
     'priced',4,1,'proxy')
on conflict (key_hash,request_id) where request_id<>'' do nothing;

do $$
declare
    v_cas_loss bigint;
begin
    v_cas_loss:=public.compare_and_set_stripe_subscription_snapshot(
        'cb100000-0000-4000-8000-000000000001',0,999,
        'evt_companybillingstale','customer.subscription.deleted',
        'sub_stale_must_not_commit','canceled',now(),now(),
        now()+interval '7 days'
    );
    if v_cas_loss is not null then
        raise exception 'stale Stripe reconciliation snapshot won a lost CAS';
    end if;
    if (select count(*)
          from public.billing_accounts
         where organization_id='cb100000-0000-4000-8000-000000000001'
           and stripe_subscription_id='sub_company_billing_fixture_a'
           and stripe_subscription_reconcile_revision=1)<>1
       or (select count(*)
             from public.billing_accounts
            where organization_id='cb200000-0000-4000-8000-000000000002'
              and stripe_subscription_id='sub_company_billing_fixture_b'
              and stripe_subscription_reconcile_revision=1)<>1 then
        raise exception 'Stripe subscription events crossed company accounts';
    end if;
    if (select count(*)
          from public.billing_ledger ledger
          join public.usage_log usage on usage.id=ledger.usage_log_id
         where usage.request_id='company-billing-usage-a'
           and ledger.organization_id='cb100000-0000-4000-8000-000000000001')<>1
       or (select count(*)
             from public.billing_ledger ledger
             join public.usage_log usage on usage.id=ledger.usage_log_id
            where usage.request_id='company-billing-usage-b'
              and ledger.organization_id='cb200000-0000-4000-8000-000000000002')<>1 then
        raise exception 'billing ledger did not preserve company isolation';
    end if;
end;
$$;
