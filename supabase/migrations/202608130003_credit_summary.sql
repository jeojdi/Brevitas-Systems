-- Credit balance + burn-down summary for the dashboard (B10).
--
-- Returns the materialized balance and rolling 7-day usage spend in one call, computed
-- server-side so the dashboard never client-side-sums a busy org's ledger. Read-only;
-- no writes. Degrades gracefully in the app layer if absent.

create or replace function public.credit_summary(p_organization_id text)
returns jsonb
language sql
security definer
set search_path = public, pg_temp
as $$
    select jsonb_build_object(
        'balance_micro', coalesce((
            select balance_micro from public.credit_balances
             where organization_id = p_organization_id), 0),
        'spent_7d_micro', coalesce((
            select sum(-amount_micro) from public.credit_ledger
             where organization_id = p_organization_id
               and entry_type = 'usage'
               and occurred_at >= now() - interval '7 days'), 0)
    );
$$;
revoke all on function public.credit_summary(text)
    from public, anon, authenticated;
grant execute on function public.credit_summary(text)
    to service_role;
