-- Fence Stripe customer persistence against concurrent billing-owner changes.
--
-- Checkout performs an external Stripe call after authorization. Human-owner
-- attribution captured before that call is stale input by the time persistence
-- begins. Keep organization_id as the Stripe and ledger identity, lock the
-- organization, and derive the compatibility user_id snapshot inside the same
-- database transaction that saves the customer.

begin;

create or replace function public.save_billing_customer_identity(
    p_organization_id uuid,
    p_stripe_customer_id text
) returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_billing_owner_id uuid;
    v_account public.billing_accounts%rowtype;
begin
    if p_organization_id is null
       or nullif(pg_catalog.btrim(p_stripe_customer_id), '') is null
       or pg_catalog.length(p_stripe_customer_id) > 255 then
        raise invalid_parameter_value using
            message = 'invalid billing customer identity parameters';
    end if;

    -- The organization lock serializes this derivation with billing-owner
    -- transfer. Locking the matching membership also prevents an owner from
    -- becoming inactive between validation and persistence.
    select organization.billing_owner_id
      into v_billing_owner_id
      from public.organizations organization
      join public.organization_members member
        on member.organization_id = organization.id
       and member.user_id = organization.billing_owner_id
     where organization.id = p_organization_id
       and organization.billing_owner_id is not null
       and member.status = 'active'
     for update of organization, member;

    if not found or v_billing_owner_id is null then
        raise check_violation using
            message = 'organization has no active billing owner';
    end if;

    insert into public.billing_accounts as account (
        organization_id,
        user_id,
        stripe_customer_id,
        updated_at
    ) values (
        p_organization_id,
        v_billing_owner_id,
        p_stripe_customer_id,
        pg_catalog.clock_timestamp()
    )
    on conflict (organization_id) do update
       set user_id = excluded.user_id,
           stripe_customer_id = excluded.stripe_customer_id,
           updated_at = pg_catalog.clock_timestamp()
     where account.stripe_customer_id is null
        or account.stripe_customer_id = excluded.stripe_customer_id
    returning account.* into v_account;

    -- Never overwrite a company with a different Stripe customer merely
    -- because a checkout request observed an earlier empty account snapshot.
    if not found then
        raise unique_violation using
            message = 'organization billing customer identity conflict';
    end if;

    return pg_catalog.to_jsonb(v_account);
end;
$function$;

revoke all on function public.save_billing_customer_identity(uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.save_billing_customer_identity(uuid, text)
    to service_role;

commit;
