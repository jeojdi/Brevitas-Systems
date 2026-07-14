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
