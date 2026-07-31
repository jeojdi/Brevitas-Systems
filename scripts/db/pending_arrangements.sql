-- Customers waiting to be attested, with the exact statement to attest each.
--
-- WHY THIS EXISTS. 202607280030 makes attestation a deliberate two-step: the
-- customer requests, a human holding the out-of-band `brevitas_attestor` role
-- attests. The request half is now opened automatically when a customer
-- completes Stripe Checkout (src/app/api/billing/webhook/route.ts), so the only
-- manual act left is the human decision -- which is the point, not an oversight.
--
-- This turns that decision into: run this, copy one line, paste it into a
-- brevitas_attestor psql session. Read-only; safe to run as the app role.
--
-- HOW TO USE:
--   supabase db query --linked -f scripts/db/pending_arrangements.sql
--   psql "host=aws-1-us-east-1.pooler.supabase.com port=5432 \
--         user=brevitas_attestor.<project_ref> dbname=postgres sslmode=require"
--   -- then paste the attest_sql line, editing the attestor name and evidence.
--
-- NOTE ON EVIDENCE: attest_billing_arrangement requires a non-trivial evidence
-- string and records it forever. The generated line names the Stripe objects
-- because that is the commercial act; replace it if you have a signed order form
-- instead. Do not paste something meaningless -- the log is the only record of
-- WHY an organization became billable.
select
    o.name                                   as organization,
    r.organization_id::text                  as organization_id,
    r.requested_at,
    r.self_declared_arrangement              as requested_arrangement,
    r.rate_presented,
    r.terms_version,
    r.agreement_reference,
    coalesce(ba.subscription_status, 'no billing account') as stripe_status,
    coalesce(a.arrangement, 'unattested')    as current_arrangement,
    -- Paste-ready. Edit 'YOUR NAME' before running it.
    format(
        'select public.attest_billing_arrangement(%L::uuid, %L, %L, %L, %L::uuid);',
        r.organization_id,
        r.self_declared_arrangement,
        'YOUR NAME',
        'stripe checkout ' || coalesce(ba.stripe_customer_id, 'unknown') ||
            ' on ' || to_char(r.requested_at, 'YYYY-MM-DD'),
        r.id
    )                                        as attest_sql
  from public.billing_arrangement_request r
  join public.organizations o
    on o.id = r.organization_id
  left join public.billing_accounts ba
    on ba.organization_id = r.organization_id
  left join public.organization_billing_arrangement a
    on a.organization_id = r.organization_id
 where r.status = 'open'
   -- Already-attested organizations are not pending work. A request that stayed
   -- 'open' behind a completed attestation is a reconciliation bug, not a queue
   -- item, and would only make the operator attest something twice.
   and a.organization_id is null
 order by r.requested_at;
