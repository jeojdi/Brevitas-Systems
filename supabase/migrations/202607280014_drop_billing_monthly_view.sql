-- Remove the legacy public.billing_monthly summary view.
--
-- The view was declared by 20260626_create_billing.sql:33 and re-created
-- verbatim by 202607270002_widen_billing_events_money.sql:34 (it had to be
-- dropped there to widen the columns it reads). It aggregates the RLS-protected
-- public.billing_events, but it carries neither `with (security_invoker = true)`
-- nor the `revoke all ... from public, anon, authenticated` that every other
-- object in this schema gets -- compare analytics.posthog_usage
-- (20260716_posthog_warehouse_view.sql:47), 202607170002_cache_security.sql:87,
-- 202607280001_cache_warming.sql:102-104 and
-- 202607280007_period_settlement_ledger.sql:232. A view without
-- security_invoker evaluates its base-table policies as the view OWNER, so
-- `using (auth.uid() = user_id)` (20260626_create_billing.sql:24-26) is never
-- applied to reads through the view, and Supabase's project default
-- `alter default privileges in schema public grant all on tables to anon,
-- authenticated` leaves it selectable with the public anon key that ships inside
-- the dashboard bundle. That combination is per-tenant call volume, savings, and
-- Brevitas revenue readable without authentication.
--
-- The view is dropped rather than hardened because it has no caller: a repo-wide
-- search for billing_monthly outside these two migrations returns nothing (no
-- api/, src/, dashboard/, brevitas/, scripts/, tests/ or docs/ reference), the
-- compliance exporters read public.billing_events directly
-- (202607170007_compliance_workflows.sql:1521,1885), and
-- scripts/db/migrate-project-data.sh copies tables only. Hardening it instead
-- would produce a relation no Data API role can read anyway: with invoker
-- semantics the caller needs SELECT on public.billing_events, which no role
-- holds.
--
-- 202607270002 is frozen and idempotent, so a from-scratch replay re-creates the
-- view and this migration drops it again; the manifest order preserves that end
-- state. Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- recreating the dropped definer view would reopen the cross-tenant anon billing leak it exists to close

begin;

drop view if exists public.billing_monthly;

do $legacy_view_removed$
begin
    if to_regclass('public.billing_monthly') is not null then
        raise exception 'legacy definer view public.billing_monthly survived'
            using errcode = '55000';
    end if;
end;
$legacy_view_removed$;

commit;
