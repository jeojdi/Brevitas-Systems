\set ON_ERROR_STOP on

do $billing_owner_transfer_race_assertions$
begin
    if not exists (
        select 1
          from public.organizations organization
          join public.billing_accounts account
            on account.organization_id=organization.id
         where organization.id='cf300000-0000-4000-8000-000000000001'
           and organization.billing_owner_id=
               'cf200000-0000-4000-8000-000000000002'
           and account.user_id=
               'cf200000-0000-4000-8000-000000000002'
           and account.stripe_customer_id='cus_owner_transfer_race'
    ) then
        raise exception 'serialized owner transfer did not win final attribution';
    end if;

    if exists (
        select 1
          from public.billing_ledger
         where organization_id='cf300000-0000-4000-8000-000000000001'
    ) then
        raise exception 'owner-transfer persistence race changed company ledger state';
    end if;
end;
$billing_owner_transfer_race_assertions$;
