-- Drop existing policies
DROP POLICY IF EXISTS "Allow anonymous inserts" ON waitlist;
DROP POLICY IF EXISTS "Allow authenticated select" ON waitlist;

-- Create a more permissive insert policy for anonymous users
CREATE POLICY "Enable insert for anon users" ON waitlist
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- Create a policy for authenticated users to select
CREATE POLICY "Enable select for authenticated users" ON waitlist
  FOR SELECT
  TO authenticated
  USING (true);

-- Also create a policy to allow anon users to check if email exists
CREATE POLICY "Enable select for anon to check email" ON waitlist
  FOR SELECT
  TO anon
  USING (true);

-- Verify the policies are created
SELECT * FROM pg_policies WHERE tablename = 'waitlist';