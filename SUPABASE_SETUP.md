# Supabase setup

Use the same Supabase project for the Next.js site, dashboard bundle, and Railway API.

## Waitlist

1. Run `supabase/create_waitlist_table.sql` in the Supabase SQL editor.
2. Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` in Vercel.
3. Restart the local Next.js server after changing `.env.local`.

Test through the server route:

```bash
curl -X POST http://localhost:3000/api/waitlist \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","company":"Test Corp"}'
```

The anonymous role may insert waitlist rows but cannot read them. Inspect or export waitlist
data only with the Supabase dashboard or a server-side service-role credential.

## Application schema

Apply every file in `supabase/migrations/` in timestamp order. Do not also apply the duplicate
base schema in `api/migrations/001_persistent_stores.sql`. Railway must use this project's
`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`; the browser uses only the public URL and anon key.

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
[the data-rights runbook](docs/compliance/DATA_RIGHTS.md). It deliberately follows company
administration migration 005 and database-scaling migration 006. Apply it only through the reviewed
migration chain; the guarded workflow fails closed if any required table/RPC is absent.
