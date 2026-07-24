-- Company-scoped Stripe billing authorization and identity.
--
-- Billing is owned by an organization, never by the human who happened to
-- open Checkout or rotate a credential. The legacy user_id columns remain as
-- non-unique billing-owner attribution so retained financial records and
-- worker payloads keep their historical actor reference.

begin;

alter table public.billing_accounts
    add column if not exists organization_id uuid
        references public.organizations(id) on delete restrict;

-- The private legacy organization is authoritative where it exists. A billing
-- owner is used as a fallback only when that user owns exactly one company.
update public.billing_accounts account
   set organization_id=organization.id
  from public.organizations organization
 where account.organization_id is null
   and organization.legacy_owner_id=account.user_id::text;

update public.billing_accounts account
   set organization_id=(
       select organization.id
         from public.organizations organization
        where organization.billing_owner_id=account.user_id
        order by organization.created_at,organization.id
        limit 1
   )
 where account.organization_id is null
   and 1=(
       select count(*)
         from public.organizations organization
        where organization.billing_owner_id=account.user_id
   );

do $$
begin
    if exists (
        select 1 from public.billing_accounts
         where organization_id is null
    ) then
        raise exception using
            errcode='23514',
            message='legacy billing account has no unambiguous company identity';
    end if;
end;
$$;

alter table public.billing_accounts
    alter column organization_id set not null;
-- On the first company-identity upgrade, replace the legacy user primary key.
-- On a completed chain, later company-scoped tables reference the already
-- correct organization primary key; do not drop that key during a replay.
do $billing_accounts_company_primary_key$
declare
    v_primary_key_definition text;
begin
    select pg_catalog.pg_get_constraintdef(constraint_state.oid)
      into v_primary_key_definition
      from pg_catalog.pg_constraint constraint_state
     where constraint_state.conrelid='public.billing_accounts'::regclass
       and constraint_state.contype='p';

    if v_primary_key_definition is distinct from 'PRIMARY KEY (organization_id)' then
        alter table public.billing_accounts
            drop constraint if exists billing_accounts_pkey;
        alter table public.billing_accounts
            add constraint billing_accounts_pkey primary key (organization_id);
    end if;
end;
$billing_accounts_company_primary_key$;
create index if not exists billing_accounts_owner_idx
    on public.billing_accounts(user_id,organization_id);

-- Keep the compatibility owner snapshot current when company ownership moves.
-- The Stripe/customer primary identity remains organization_id throughout.
create or replace function public.sync_company_billing_owner_snapshot()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if new.billing_owner_id is distinct from old.billing_owner_id
       and new.billing_owner_id is not null then
        update public.billing_accounts
           set user_id=new.billing_owner_id,updated_at=clock_timestamp()
         where organization_id=new.id;
    end if;
    return new;
end;
$$;
revoke all on function public.sync_company_billing_owner_snapshot()
    from public, anon, authenticated;
drop trigger if exists sync_company_billing_owner_snapshot
    on public.organizations;
create trigger sync_company_billing_owner_snapshot
after update of billing_owner_id on public.organizations
for each row execute function public.sync_company_billing_owner_snapshot();

alter table public.billing_ledger
    add column if not exists organization_id uuid
        references public.organizations(id) on delete restrict;

-- Usage already carries the server-derived tenant. This is the strongest
-- possible source for retained ledger rows and does not infer from a mutable
-- human membership.
update public.billing_ledger ledger
   set organization_id=usage.organization_id
  from public.usage_log usage
 where ledger.organization_id is null
   and usage.id=ledger.usage_log_id
   and usage.organization_id is not null;

do $$
begin
    if exists (
        select 1 from public.billing_ledger
         where organization_id is null
    ) then
        raise exception using
            errcode='23514',
            message='retained billing ledger row has no authoritative company identity';
    end if;
end;
$$;

alter table public.billing_ledger
    alter column organization_id set not null;
create index if not exists billing_ledger_company_period_idx
    on public.billing_ledger(organization_id,occurred_at);

-- Resolve the actor's server-owned active-company selection, then authorize
-- from the locked active membership and the canonical permission function.
-- No organization identifier is accepted from HTTP.
create or replace function public.company_billing_authorize_actor(
    p_actor_user_id uuid
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_selection jsonb;
    v_organization_id uuid;
    v_role text;
    v_billing_owner_id uuid;
begin
    v_selection := public.company_admin_resolve_active_membership(p_actor_user_id);
    if coalesce((v_selection->>'ok')::boolean,false) is not true then
        return jsonb_build_object(
            'ok',false,
            'code',coalesce(v_selection->>'code','no_active_membership')
        );
    end if;

    v_organization_id := (v_selection->>'company_id')::uuid;
    select member.role,organization.billing_owner_id
      into v_role,v_billing_owner_id
      from public.organization_members member
      join public.organizations organization
        on organization.id=member.organization_id
     where member.organization_id=v_organization_id
       and member.user_id=p_actor_user_id
       and member.status='active'
     for share of member,organization;

    if v_role is null
       or not ('billing:manage'=any(public.company_role_permissions(v_role))) then
        return jsonb_build_object('ok',false,'code','forbidden');
    end if;
    if v_billing_owner_id is null then
        return jsonb_build_object('ok',false,'code','billing_owner_unavailable');
    end if;

    return jsonb_build_object(
        'ok',true,
        'code','authorized',
        'organization_id',v_organization_id,
        'billing_owner_id',v_billing_owner_id,
        'role',v_role
    );
end;
$$;
revoke all on function public.company_billing_authorize_actor(uuid)
    from public, anon, authenticated;
grant execute on function public.company_billing_authorize_actor(uuid)
    to service_role;

-- New usage is eligible only against its server-derived organization account.
create or replace function public.queue_brevitas_fee()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    safe_fee numeric;
    v_billing_owner_id uuid;
begin
    if not new.authoritative
       or new.organization_id is null
       or new.owner_id=''
       or new.pricing_status<>'priced' then
        return new;
    end if;

    select organization.billing_owner_id into v_billing_owner_id
      from public.billing_accounts account
      join public.organizations organization
        on organization.id=account.organization_id
     where account.organization_id=new.organization_id
       and account.subscription_status in ('active','trialing')
       and account.billing_started_at is not null
       and new.ts>=account.billing_started_at;
    if v_billing_owner_id is null then
        return new;
    end if;

    safe_fee := least(
        greatest(coalesce(new.brevitas_fee_usd,0),0),
        greatest(coalesce(new.verified_savings_usd,0),0)*0.25
    );
    insert into public.billing_ledger(
        usage_log_id,organization_id,user_id,occurred_at,fee_microusd
    ) values (
        new.id,new.organization_id,v_billing_owner_id,new.ts,
        floor(safe_fee*1000000)::bigint
    ) on conflict (usage_log_id) do nothing;
    return new;
end;
$$;
revoke all on function public.queue_brevitas_fee()
    from public, anon, authenticated;

-- Preserve the legacy single-entry API, but scope its account lock and cap to
-- the immutable organization carried by the ledger row.
create or replace function public.claim_billing_ledger_entry(
    p_entry_id bigint,
    p_cap_microusd bigint
) returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    entry public.billing_ledger%rowtype;
    account public.billing_accounts%rowtype;
    committed bigint;
    period_start timestamptz;
    period_end timestamptz;
begin
    select * into entry from public.billing_ledger where id=p_entry_id for update;
    if not found or entry.status<>'pending' then return 'unavailable'; end if;
    perform pg_advisory_xact_lock(hashtextextended(entry.organization_id::text,0));
    select * into account from public.billing_accounts
     where organization_id=entry.organization_id;
    if not found or account.subscription_status not in ('active','trialing') then
        return 'inactive';
    end if;
    if account.current_period_start is null
       or account.current_period_end-account.current_period_start<>interval '7 days' then
        update public.billing_ledger
           set status='review',last_error='invalid Stripe weekly billing-period anchor'
         where id=entry.id;
        return 'review';
    end if;
    period_start := account.current_period_start
        + floor(extract(epoch from (entry.occurred_at-account.current_period_start))/604800)
          * interval '7 days';
    period_end := period_start+interval '7 days';
    select coalesce(sum(fee_microusd),0) into committed
      from public.billing_ledger
     where organization_id=entry.organization_id
       and occurred_at>=period_start and occurred_at<period_end
       and status in ('sending','reported','review');
    if committed+entry.fee_microusd>p_cap_microusd then
        update public.billing_ledger
           set status='capped',last_error='weekly safety cap reached'
         where id=entry.id;
        return 'capped';
    end if;
    update public.billing_ledger
       set status='sending',attempts=attempts+1,last_error=''
     where id=entry.id;
    return 'sending';
end;
$$;
revoke all on function public.claim_billing_ledger_entry(bigint,bigint)
    from public, anon, authenticated;
grant execute on function public.claim_billing_ledger_entry(bigint,bigint)
    to service_role;

-- The recovery worker retains its payload contract. Its concurrency lock,
-- customer lookup, committed sum, and weekly cap are all company-scoped.
create or replace function public.claim_billing_ledger_entries(
    p_owner text,
    p_lease_seconds integer,
    p_limit integer,
    p_cap_microusd bigint
) returns table (
    id bigint,
    user_id uuid,
    occurred_at timestamptz,
    fee_microusd bigint,
    stripe_customer_id text,
    attempts integer,
    reclaimed boolean,
    outbound_started_at timestamptz,
    period_start timestamptz,
    period_end timestamptz,
    expected_period_microusd bigint
)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    candidate public.billing_ledger%rowtype;
    account public.billing_accounts%rowtype;
    committed bigint;
    claim_period_start timestamptz;
    claim_period_end timestamptz;
    was_reclaimed boolean;
begin
    if nullif(btrim(p_owner),'') is null
       or p_lease_seconds not between 15 and 900
       or p_limit<>1
       or p_cap_microusd<=0 then
        raise exception 'invalid billing claim parameters';
    end if;

    update public.billing_ledger ledger
       set status='expired',last_error='Stripe 35-day reporting window elapsed',
           lease_owner=null,lease_expires_at=null
     where ledger.status='pending'
       and ledger.occurred_at<now()-interval '34 days';
    update public.billing_ledger ledger
       set status='review',last_error='ambiguous Stripe send exceeded safe replay window',
           lease_owner=null,lease_expires_at=null
     where ledger.status='sending' and ledger.lease_expires_at<now()
       and ledger.outbound_started_at<now()-interval '23 hours';
    update public.billing_ledger ledger
       set status='review',last_error='billing recovery attempts exhausted',
           lease_owner=null,lease_expires_at=null
     where ledger.status='sending' and ledger.lease_expires_at<now()
       and ledger.attempts>=ledger.max_attempts;

    for candidate in
        select ledger.* from public.billing_ledger ledger
         where ledger.attempts<ledger.max_attempts
           and ledger.next_attempt_at<=now()
           and (ledger.status='pending'
                or (ledger.status='sending' and ledger.lease_expires_at<now()))
         order by ledger.next_attempt_at,ledger.occurred_at,ledger.id
         for update skip locked limit p_limit
    loop
        was_reclaimed := candidate.status='sending';
        perform pg_advisory_xact_lock(
            hashtextextended(candidate.organization_id::text,0));
        select billing_account.* into account
          from public.billing_accounts billing_account
         where billing_account.organization_id=candidate.organization_id;
        if not found or account.stripe_customer_id is null
           or account.subscription_status not in ('active','trialing') then
            update public.billing_ledger
               set status='dead',last_error='billable Stripe account is unavailable',
                   lease_owner=null,lease_expires_at=null
             where billing_ledger.id=candidate.id;
            continue;
        end if;

        begin
            select period.period_start,period.period_end
              into claim_period_start,claim_period_end
              from public.billing_period_for_occurrence(
                  candidate.occurred_at,account.current_period_start,
                  account.current_period_end
              ) period;
        exception when invalid_parameter_value then
            update public.billing_ledger
               set status='review',last_error='invalid Stripe weekly billing-period anchor',
                   lease_owner=null,lease_expires_at=null
             where billing_ledger.id=candidate.id;
            continue;
        end;

        select coalesce(sum(ledger.fee_microusd),0) into committed
          from public.billing_ledger ledger
         where ledger.organization_id=candidate.organization_id
           and ledger.occurred_at>=claim_period_start
           and ledger.occurred_at<claim_period_end
           and ledger.status in ('sending','reported','review');
        if not was_reclaimed
           and committed+candidate.fee_microusd>p_cap_microusd then
            update public.billing_ledger
               set status='capped',last_error='weekly safety cap reached',
                   lease_owner=null,lease_expires_at=null
             where billing_ledger.id=candidate.id;
            continue;
        end if;

        update public.billing_ledger
           set status='sending',attempts=billing_ledger.attempts+1,
               lease_owner=p_owner,
               lease_expires_at=now()+make_interval(secs=>p_lease_seconds),
               last_error=''
         where billing_ledger.id=candidate.id;
        if not was_reclaimed then committed:=committed+candidate.fee_microusd; end if;
        return query select candidate.id,candidate.user_id,candidate.occurred_at,
            candidate.fee_microusd,account.stripe_customer_id,
            candidate.attempts+1,was_reclaimed,candidate.outbound_started_at,
            claim_period_start,claim_period_end,committed;
    end loop;
end;
$$;
revoke all on function public.claim_billing_ledger_entries(text,integer,integer,bigint)
    from public, anon, authenticated;
grant execute on function public.claim_billing_ledger_entries(text,integer,integer,bigint)
    to service_role;

-- Reconciliation CAS is company-scoped after the identity migration. Drop the
-- user-parameter form first because PostgREST calls functions by named inputs.
drop function if exists public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
);
create function public.compare_and_set_stripe_subscription_snapshot(
    p_organization_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_id text,
    p_event_type text,
    p_stripe_subscription_id text,
    p_subscription_status text,
    p_billing_started_at timestamptz,
    p_current_period_start timestamptz,
    p_current_period_end timestamptz
) returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare v_revision bigint;
begin
    if p_organization_id is null
       or p_expected_revision is null or p_expected_revision<0
       or p_event_created is null or p_event_created<0
       or p_event_id is null or p_event_id!~'^evt_[A-Za-z0-9]+$'
       or length(p_event_id)>255
       or p_event_type not in (
           'checkout.session.completed','customer.subscription.created',
           'customer.subscription.updated','customer.subscription.deleted') then
        raise exception using errcode='22023',
            message='invalid Stripe subscription reconciliation snapshot';
    end if;
    update public.billing_accounts account
       set stripe_subscription_id=p_stripe_subscription_id,
           subscription_status=p_subscription_status,
           billing_started_at=p_billing_started_at,
           current_period_start=p_current_period_start,
           current_period_end=p_current_period_end,
           stripe_subscription_event_created=p_event_created,
           stripe_subscription_event_id=p_event_id,
           stripe_subscription_event_type=p_event_type,
           stripe_subscription_reconcile_revision=
               account.stripe_subscription_reconcile_revision+1,
           updated_at=clock_timestamp()
     where account.organization_id=p_organization_id
       and account.stripe_subscription_reconcile_revision=p_expected_revision
    returning stripe_subscription_reconcile_revision into v_revision;
    return v_revision;
end;
$$;
revoke all on function public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
) from public, anon, authenticated;
grant execute on function public.compare_and_set_stripe_subscription_snapshot(
    uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz
) to service_role;

drop function if exists public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
);
create function public.compare_and_set_stripe_invoice_snapshot(
    p_organization_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_id text,
    p_event_type text,
    p_last_invoice_id text,
    p_last_invoice_status text
) returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare v_revision bigint;
begin
    if p_organization_id is null
       or p_expected_revision is null or p_expected_revision<0
       or p_event_created is null or p_event_created<0
       or p_event_id is null or p_event_id!~'^evt_[A-Za-z0-9]+$'
       or length(p_event_id)>255
       or p_event_type not in ('invoice.payment_failed','invoice.paid') then
        raise exception using errcode='22023',
            message='invalid Stripe invoice reconciliation snapshot';
    end if;
    update public.billing_accounts account
       set last_invoice_id=p_last_invoice_id,
           last_invoice_status=p_last_invoice_status,
           stripe_invoice_event_created=p_event_created,
           stripe_invoice_event_id=p_event_id,
           stripe_invoice_event_type=p_event_type,
           stripe_invoice_reconcile_revision=
               account.stripe_invoice_reconcile_revision+1,
           updated_at=clock_timestamp()
     where account.organization_id=p_organization_id
       and account.stripe_invoice_reconcile_revision=p_expected_revision
    returning stripe_invoice_reconcile_revision into v_revision;
    return v_revision;
end;
$$;
revoke all on function public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
) from public, anon, authenticated;
grant execute on function public.compare_and_set_stripe_invoice_snapshot(
    uuid,bigint,bigint,text,text,text,text
) to service_role;

-- Organization identity joins the immutable ledger identity after backfill.
create or replace function public.prevent_billing_ledger_identity_change()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if new.id is distinct from old.id
       or new.usage_log_id is distinct from old.usage_log_id
       or new.organization_id is distinct from old.organization_id
       or new.user_id is distinct from old.user_id
       or new.occurred_at is distinct from old.occurred_at
       or new.fee_microusd is distinct from old.fee_microusd
       or new.created_at is distinct from old.created_at then
        raise exception 'billing ledger identity, amount, source, and creation fields are immutable';
    end if;
    return new;
end;
$$;
revoke all on function public.prevent_billing_ledger_identity_change()
    from public, anon, authenticated;

-- Contract assertions: one owner may bill independently for multiple companies
-- and only canonical billing roles possess billing:manage.
do $$
begin
    if not ('billing:manage'=any(public.company_role_permissions('company_owner')))
       or not ('billing:manage'=any(public.company_role_permissions('billing_admin')))
       or 'billing:manage'=any(public.company_role_permissions('company_admin'))
       or 'billing:manage'=any(public.company_role_permissions('member')) then
        raise exception 'company billing role contract is invalid';
    end if;
end;
$$;

commit;
