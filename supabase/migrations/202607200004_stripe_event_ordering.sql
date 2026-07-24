-- Optimistic compare-and-set storage for Stripe reconciliation snapshots.
-- Stripe event metadata is retained only for diagnostics. Event time, type,
-- and identifier never authorize or suppress canonical business state.

begin;

alter table public.billing_accounts
    add column if not exists stripe_subscription_reconcile_revision bigint not null default 0,
    add column if not exists stripe_subscription_event_id text not null default '',
    add column if not exists stripe_subscription_event_type text not null default '',
    add column if not exists stripe_invoice_reconcile_revision bigint not null default 0,
    add column if not exists stripe_invoice_event_id text not null default '',
    add column if not exists stripe_invoice_event_type text not null default '';

do $$
begin
    alter table public.billing_accounts
        add constraint billing_accounts_subscription_reconcile_check
        check (
            stripe_subscription_reconcile_revision >= 0
            and (
                stripe_subscription_event_id = ''
                or (
                    length(stripe_subscription_event_id) <= 255
                    and stripe_subscription_event_id ~ '^evt_[A-Za-z0-9]+$'
                )
            )
            and stripe_subscription_event_type in (
                '',
                'checkout.session.completed',
                'customer.subscription.created',
                'customer.subscription.updated',
                'customer.subscription.deleted'
            )
        );
exception when duplicate_object then null;
end;
$$;

do $$
begin
    alter table public.billing_accounts
        add constraint billing_accounts_invoice_reconcile_check
        check (
            stripe_invoice_reconcile_revision >= 0
            and (
                stripe_invoice_event_id = ''
                or (
                    length(stripe_invoice_event_id) <= 255
                    and stripe_invoice_event_id ~ '^evt_[A-Za-z0-9]+$'
                )
            )
            and stripe_invoice_event_type in (
                '', 'invoice.payment_failed', 'invoice.paid'
            )
        );
exception when duplicate_object then null;
end;
$$;

-- PostgreSQL does not permit CREATE OR REPLACE to rename input parameters for
-- an otherwise identical signature. Drop first so this maintenance migration
-- remains transactionally rerunnable after the company-scoped successor has
-- used p_organization_id for the same UUID signature.
drop function if exists public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
);
create function public.compare_and_set_stripe_subscription_snapshot(
    p_user_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_id text,
    p_event_type text,
    p_stripe_subscription_id text,
    p_subscription_status text,
    p_billing_started_at timestamptz,
    p_current_period_start timestamptz,
    p_current_period_end timestamptz
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_revision bigint;
begin
    if p_user_id is null
       or p_expected_revision is null or p_expected_revision < 0
       or p_event_created is null or p_event_created < 0
       or p_event_id is null or p_event_id !~ '^evt_[A-Za-z0-9]+$'
       or length(p_event_id) > 255
       or p_event_type not in (
           'checkout.session.completed',
           'customer.subscription.created',
           'customer.subscription.updated',
           'customer.subscription.deleted'
       ) then
        raise exception using
            errcode = '22023',
            message = 'invalid Stripe subscription reconciliation snapshot';
    end if;

    update public.billing_accounts account
       set stripe_subscription_id = p_stripe_subscription_id,
           subscription_status = p_subscription_status,
           billing_started_at = p_billing_started_at,
           current_period_start = p_current_period_start,
           current_period_end = p_current_period_end,
           stripe_subscription_event_created = p_event_created,
           stripe_subscription_event_id = p_event_id,
           stripe_subscription_event_type = p_event_type,
           stripe_subscription_reconcile_revision =
               account.stripe_subscription_reconcile_revision + 1,
           updated_at = clock_timestamp()
     where account.user_id = p_user_id
       and account.stripe_subscription_reconcile_revision = p_expected_revision
    returning stripe_subscription_reconcile_revision into v_revision;
    return v_revision;
end;
$$;

drop function if exists public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
);
create function public.compare_and_set_stripe_invoice_snapshot(
    p_user_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_id text,
    p_event_type text,
    p_last_invoice_id text,
    p_last_invoice_status text
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_revision bigint;
begin
    if p_user_id is null
       or p_expected_revision is null or p_expected_revision < 0
       or p_event_created is null or p_event_created < 0
       or p_event_id is null or p_event_id !~ '^evt_[A-Za-z0-9]+$'
       or length(p_event_id) > 255
       or p_event_type not in ('invoice.payment_failed', 'invoice.paid') then
        raise exception using
            errcode = '22023',
            message = 'invalid Stripe invoice reconciliation snapshot';
    end if;

    update public.billing_accounts account
       set last_invoice_id = p_last_invoice_id,
           last_invoice_status = p_last_invoice_status,
           stripe_invoice_event_created = p_event_created,
           stripe_invoice_event_id = p_event_id,
           stripe_invoice_event_type = p_event_type,
           stripe_invoice_reconcile_revision =
               account.stripe_invoice_reconcile_revision + 1,
           updated_at = clock_timestamp()
     where account.user_id = p_user_id
       and account.stripe_invoice_reconcile_revision = p_expected_revision
    returning stripe_invoice_reconcile_revision into v_revision;
    return v_revision;
end;
$$;

revoke all on function public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
) from public, anon, authenticated;
revoke all on function public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
) from public, anon, authenticated;
grant execute on function public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
) to service_role;
grant execute on function public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
) to service_role;

commit;
