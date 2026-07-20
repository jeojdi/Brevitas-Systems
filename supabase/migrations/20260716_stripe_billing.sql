-- Stripe billing state and an append-only, fail-safe usage ledger.
--
-- Money sent to Stripe is represented as whole micro-dollars. The trigger floors
-- each fee and caps it at 25% of verified savings, so rounding and corrupted
-- application values can never increase a customer's charge.

create table if not exists public.billing_accounts (
    user_id uuid primary key references auth.users(id) on delete cascade,
    stripe_customer_id text unique,
    stripe_subscription_id text unique,
    subscription_status text not null default 'not_started',
    checkout_session_id text,
    billing_started_at timestamptz,
    current_period_start timestamptz,
    current_period_end timestamptz,
    last_invoice_id text,
    last_invoice_status text,
    stripe_subscription_event_created bigint not null default 0,
    stripe_invoice_event_created bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.billing_ledger (
    id bigint generated always as identity primary key,
    usage_log_id bigint not null unique references public.usage_log(id) on delete restrict,
    user_id uuid not null references auth.users(id) on delete restrict,
    occurred_at timestamptz not null,
    fee_microusd bigint not null check (fee_microusd >= 0),
    status text not null default 'pending'
        check (status in ('pending', 'sending', 'reported', 'review', 'capped', 'expired')),
    attempts integer not null default 0,
    reported_at timestamptz,
    last_error text not null default '',
    created_at timestamptz not null default now()
);

create index if not exists billing_ledger_pending_idx
    on public.billing_ledger (status, occurred_at, id);
create index if not exists billing_ledger_user_period_idx
    on public.billing_ledger (user_id, occurred_at);

create table if not exists public.stripe_webhook_events (
    event_id text primary key,
    event_type text not null,
    processed_at timestamptz not null default now()
);

alter table public.billing_accounts enable row level security;
alter table public.billing_ledger enable row level security;
alter table public.stripe_webhook_events enable row level security;

-- These tables are service-owned. Intentionally grant no browser policies.

create or replace function public.queue_brevitas_fee()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    safe_fee numeric;
begin
    if new.owner_id = '' or new.pricing_status <> 'priced' then
        return new;
    end if;

    if not exists (
        select 1
        from public.billing_accounts account
        where account.user_id::text = new.owner_id
          and account.subscription_status in ('active', 'trialing')
          and account.billing_started_at is not null
          and new.ts >= account.billing_started_at
    ) then
        return new;
    end if;

    safe_fee := least(
        greatest(coalesce(new.brevitas_fee_usd, 0), 0),
        greatest(coalesce(new.verified_savings_usd, 0), 0) * 0.25
    );

    insert into public.billing_ledger (usage_log_id, user_id, occurred_at, fee_microusd)
    values (new.id, new.owner_id::uuid, new.ts, floor(safe_fee * 1000000)::bigint)
    on conflict (usage_log_id) do nothing;

    return new;
exception
    when invalid_text_representation then
        -- Legacy/non-Supabase owner IDs are never billable.
        return new;
end;
$$;

drop trigger if exists queue_brevitas_fee_after_usage on public.usage_log;
create trigger queue_brevitas_fee_after_usage
after insert on public.usage_log
for each row execute function public.queue_brevitas_fee();

-- Claim one ledger row under a per-account transaction lock. Ambiguous Stripe
-- failures move to `review` and are never retried automatically; this deliberately
-- prefers undercharging over the possibility of sending the same value twice.
create or replace function public.claim_billing_ledger_entry(
    p_entry_id bigint,
    p_cap_microusd bigint
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    entry public.billing_ledger%rowtype;
    account public.billing_accounts%rowtype;
    committed bigint;
    period_start timestamptz;
    period_end timestamptz;
begin
    select * into entry from public.billing_ledger where id = p_entry_id for update;
    if not found or entry.status <> 'pending' then
        return 'unavailable';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(entry.user_id::text, 0));
    select * into account from public.billing_accounts where user_id = entry.user_id;
    if not found or account.subscription_status not in ('active', 'trialing') then
        return 'inactive';
    end if;

    if account.current_period_start is null
       or account.current_period_end - account.current_period_start <> interval '7 days' then
        update public.billing_ledger
        set status = 'review', last_error = 'invalid Stripe weekly billing-period anchor'
        where id = entry.id;
        return 'review';
    end if;
    period_start := account.current_period_start
        + floor(extract(epoch from (entry.occurred_at - account.current_period_start)) / 604800)
          * interval '7 days';
    period_end := period_start + interval '7 days';
    select coalesce(sum(fee_microusd), 0) into committed
    from public.billing_ledger
    where user_id = entry.user_id
      and occurred_at >= period_start
      and occurred_at < period_end
      and status in ('sending', 'reported', 'review');

    if committed + entry.fee_microusd > p_cap_microusd then
        update public.billing_ledger
        set status = 'capped', last_error = 'weekly safety cap reached'
        where id = entry.id;
        return 'capped';
    end if;

    update public.billing_ledger
    set status = 'sending', attempts = attempts + 1, last_error = ''
    where id = entry.id;
    return 'sending';
end;
$$;

revoke all on function public.queue_brevitas_fee() from public, anon, authenticated;
revoke all on function public.claim_billing_ledger_entry(bigint, bigint) from public, anon, authenticated;
grant execute on function public.claim_billing_ledger_entry(bigint, bigint) to service_role;
