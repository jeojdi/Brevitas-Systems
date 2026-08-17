-- Credit ledger for credit-based pricing (B5/B7; see CREDIT_PRICING_PLAN.md).
--
-- Append-only source of truth (credit_ledger) plus a materialized per-org balance
-- (credit_balances) that is the O(1) atomic-decrement target. Ships DARK: the gateway
-- only debits when BREVITAS_CREDIT_PRICE_MICRO is set, so until then these tables stay
-- inert. Idempotency is fenced by partial unique indexes: one usage debit per request_id,
-- one grant per stripe_event_id, one trial grant per organization.
--
-- Amounts are integer micro-credits (1 credit = $0.000001), matching the existing µUSD
-- meter, so all arithmetic is exact and race-safe. Balances may go negative (soft
-- overage) because a BYO-key request already cost the caller at the provider.

create table if not exists public.credit_ledger (
    id bigint generated always as identity primary key,
    organization_id text not null,
    customer_id text not null default '',
    entry_type text not null check (entry_type in ('usage','grant','purchase','refund','adjustment')),
    amount_micro bigint not null,
    request_id text not null default '',
    stripe_event_id text not null default '',
    reason text not null default '',
    occurred_at timestamptz not null default now()
);
create unique index if not exists credit_ledger_usage_request_idx
    on public.credit_ledger(request_id) where entry_type='usage' and request_id<>'';
create unique index if not exists credit_ledger_event_idx
    on public.credit_ledger(stripe_event_id) where stripe_event_id<>'';
create unique index if not exists credit_ledger_trial_idx
    on public.credit_ledger(organization_id) where entry_type='grant' and reason='trial';
create index if not exists credit_ledger_org_idx
    on public.credit_ledger(organization_id, occurred_at desc, id desc);

create table if not exists public.credit_balances (
    organization_id text primary key,
    balance_micro bigint not null default 0,
    updated_at timestamptz not null default now()
);

alter table public.credit_ledger enable row level security;
alter table public.credit_balances enable row level security;
grant select, insert on public.credit_ledger to service_role;
grant select, insert, update on public.credit_balances to service_role;
grant usage, select on sequence public.credit_ledger_id_seq to service_role;

-- Atomic, idempotent per-request debit. The ledger insert (fenced by the request_id
-- partial unique index) and the balance decrement are one statement-pair in one function
-- call; concurrent calls serialize on the balance row and a request never double-charges.
create or replace function public.debit_credits_for_request(
    p_organization_id text,
    p_customer_id text,
    p_request_id text,
    p_amount_micro bigint
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if p_organization_id is null or p_organization_id = ''
       or p_request_id is null or p_request_id = ''
       or coalesce(p_amount_micro, 0) <= 0 then
        return false;
    end if;
    insert into public.credit_ledger(
        organization_id, customer_id, entry_type, amount_micro, request_id, occurred_at)
    values (
        p_organization_id, coalesce(p_customer_id, ''), 'usage',
        -p_amount_micro, p_request_id, now())
    on conflict do nothing;
    if not found then
        return false;  -- this request was already debited
    end if;
    insert into public.credit_balances(organization_id, balance_micro, updated_at)
    values (p_organization_id, -p_amount_micro, now())
    on conflict (organization_id) do update
        set balance_micro = public.credit_balances.balance_micro - p_amount_micro,
            updated_at = now();
    return true;
end;
$$;
revoke all on function public.debit_credits_for_request(text,text,text,bigint)
    from public, anon, authenticated;
grant execute on function public.debit_credits_for_request(text,text,text,bigint)
    to service_role;

-- Add credits (trial grant / Stripe purchase / adjustment). Idempotent on stripe_event_id
-- when provided, and one-per-org for a trial grant (reason='trial').
create or replace function public.grant_credits(
    p_organization_id text,
    p_amount_micro bigint,
    p_entry_type text default 'grant',
    p_reason text default '',
    p_stripe_event_id text default '',
    p_customer_id text default ''
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if p_organization_id is null or p_organization_id = ''
       or coalesce(p_amount_micro, 0) <= 0
       or coalesce(p_entry_type,'') not in ('grant','purchase','refund','adjustment') then
        return false;
    end if;
    insert into public.credit_ledger(
        organization_id, customer_id, entry_type, amount_micro,
        stripe_event_id, reason, occurred_at)
    values (
        p_organization_id, coalesce(p_customer_id,''), p_entry_type, p_amount_micro,
        coalesce(p_stripe_event_id,''), coalesce(p_reason,''), now())
    on conflict do nothing;
    if not found then
        return false;  -- duplicate stripe_event_id or trial already granted
    end if;
    insert into public.credit_balances(organization_id, balance_micro, updated_at)
    values (p_organization_id, p_amount_micro, now())
    on conflict (organization_id) do update
        set balance_micro = public.credit_balances.balance_micro + p_amount_micro,
            updated_at = now();
    return true;
end;
$$;
revoke all on function public.grant_credits(text,bigint,text,text,text,text)
    from public, anon, authenticated;
grant execute on function public.grant_credits(text,bigint,text,text,text,text)
    to service_role;
