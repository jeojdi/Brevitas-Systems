\set ON_ERROR_STOP on

-- Representative rows from the documented production baseline (all forward
-- Supabase migrations through 20260716_stripe_billing_rate_25pct).
insert into auth.users(id, email) values (
    'e0000000-0000-4000-8000-000000000001',
    'upgrade-baseline@example.invalid'
) on conflict (id) do nothing;

insert into public.user_keys(user_id, api_key) values (
    'e0000000-0000-4000-8000-000000000001',
    'raw-browser-key-must-be-destroyed'
) on conflict (user_id) do update set api_key = excluded.api_key;

insert into public.api_keys(key_hash, name, owner_id) values (
    'upgrade-baseline-key', 'Upgrade baseline key',
    'e0000000-0000-4000-8000-000000000001'
) on conflict (key_hash) do nothing;

insert into public.billing_accounts(
    user_id, stripe_customer_id, subscription_status, billing_started_at,
    current_period_start, current_period_end
) values (
    'e0000000-0000-4000-8000-000000000001', 'cus_upgrade_baseline', 'active',
    '2026-01-01 00:00:00+00', '2026-07-01 00:00:00+00', '2026-08-01 00:00:00+00'
) on conflict (user_id) do update set subscription_status = 'active';

insert into public.usage_log(
    key_hash, owner_id, ts, request_id, project,
    baseline_tokens, optimized_tokens, tokens_saved, pricing_status,
    verified_savings_usd, brevitas_fee_usd, receipt_source
) values (
    'upgrade-baseline-key', 'e0000000-0000-4000-8000-000000000001',
    '2026-07-15 12:00:00+00', 'upgrade-baseline-usage', 'Upgrade baseline',
    100, 50, 50, 'priced', 1.0, 0.25, 'proxy'
) on conflict (key_hash, request_id) where request_id <> '' do nothing;

do $$
begin
    if (select count(*) from public.billing_ledger ledger
         join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id = 'upgrade-baseline-usage') <> 1 then
        raise exception 'known baseline did not create its financial ledger fixture';
    end if;
end;
$$;
