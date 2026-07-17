-- Move Brevitas billing from 10% to 25% of positive verified savings.
--
-- Replacing the function updates the existing trigger in place. New usage rows
-- created after this migration are capped at the new rate; historical ledger
-- entries remain unchanged.

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

revoke all on function public.queue_brevitas_fee() from public, anon, authenticated;
