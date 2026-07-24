-- Emergency compatibility guard only. The tracked migration chain owns the
-- server-only submit_waitlist_signup RPC and is the deployment source of truth.
DROP POLICY IF EXISTS "Allow anonymous inserts" ON public.waitlist;
DROP POLICY IF EXISTS "Allow authenticated select" ON public.waitlist;
DROP POLICY IF EXISTS "Enable insert for anon users" ON public.waitlist;
DROP POLICY IF EXISTS "Enable select for authenticated users" ON public.waitlist;
DROP POLICY IF EXISTS "Enable select for anon to check email" ON public.waitlist;

REVOKE ALL ON TABLE public.waitlist FROM PUBLIC, anon, authenticated;
