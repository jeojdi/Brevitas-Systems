# Supabase setup

Use the same Supabase project for the Next.js site, dashboard bundle, and Railway API.

## Waitlist

1. Apply the complete release manifest through its final migration,
   `supabase/migrations/202607200017_billing_customer_owner_fencing.sql`, in
   order. The preceding `202607200010_shared_endpoint_rate_limits.sql` must be
   present before deploying the waitlist or manual billing-recovery routes that
   depend on it. Without `202607200010`, the affected route fails closed with
   `503` and does not accept mutations. Do not bootstrap production from the
   legacy root-level SQL helpers.
2. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` as server-only Vercel
   secrets. Keep `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`
   for browser authentication only.
3. Restart the Next.js server after changing its environment.

Test through the server route:

```bash
curl -X POST http://localhost:3000/api/waitlist \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","company":"Test Corp"}'
```

The anonymous and authenticated roles cannot read or insert waitlist rows or
invoke the submission function. Public signups must pass through `/api/waitlist`,
which rate-limits and validates the request before using its server-only credential.
Inspect or export waitlist data only with the Supabase dashboard.

## Application schema

Apply the reviewed release manifest in order, including the `202607200013`
shared billing-control limiter, `202607200014` Checkout generation reservation,
the `202607200015` durable provider-outbound fence, `202607200016` receipt-bound
durable onboarding, the final `202607200017` billing-owner/customer persistence
fence, the `202607200012` Stripe webhook lease-renewal migration, the `202607200011` compliance
billing-isolation migration, and the preceding `202607200010` shared-endpoint
limiter. Do not also apply the duplicate base
schema in `api/migrations/001_persistent_stores.sql`. Railway must use this
project's `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`; the browser uses only
the public URL and anon key.

## Enterprise production and recovery

Production requires Supabase Team or Enterprise in the same primary US region as Railway and Redis
Cloud. Enable Supavisor pooling for application traffic and 14-day PITR. Application pool URLs are
not backup URLs: provide the restricted backup runner a dedicated, direct Postgres connection with
least privilege and managed-secret injection. Never commit or print either URL.

Maintain a separate encrypted logical backup every day, retain it for 35 days, and exercise an
isolated restore quarterly. The logical restore target is a separately created PostgreSQL 16
database in explicit `ephemeral-postgres` mode with the documented compatibility roles/extensions;
it is not a fresh Supabase project. Every restore requires a separately protected, source-bound
deletion artifact newer than the backup and must replay it before readiness, including when it
contains zero tombstones. Repository commands default to offline dry-run and do not provision a
project, enable PITR, apply a migration, or connect unless an operator supplies explicit apply flags
and named environment credentials. Follow [the disaster-recovery runbook](docs/enterprise/DISASTER_RECOVERY.md)
and retain its table-level evidence.

Tenant export/deletion uses the separate ordered migration
`supabase/migrations/202607170007_compliance_workflows.sql` and
its company-billing isolation successor
`supabase/migrations/202607200011_compliance_billing_isolation.sql`, plus
[the data-rights runbook](docs/compliance/DATA_RIGHTS.md). It deliberately follows company
administration migration 005 and database-scaling migration 006. Apply it only through the reviewed
migration chain through `202607200017`; the guarded workflow fails closed if any required table/RPC
is absent.
