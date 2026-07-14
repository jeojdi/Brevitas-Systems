-- Public submissions may insert, but no browser role may read waitlist PII.
DROP POLICY IF EXISTS "Allow anonymous inserts" ON public.waitlist;
DROP POLICY IF EXISTS "Allow authenticated select" ON public.waitlist;
DROP POLICY IF EXISTS "Enable insert for anon users" ON public.waitlist;
DROP POLICY IF EXISTS "Enable select for authenticated users" ON public.waitlist;
DROP POLICY IF EXISTS "Enable select for anon to check email" ON public.waitlist;

CREATE POLICY "Enable insert for anon users" ON public.waitlist
  FOR INSERT TO anon WITH CHECK (true);
