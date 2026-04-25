-- Create waitlist table with enhanced security
CREATE TABLE IF NOT EXISTS waitlist (
  id BIGSERIAL PRIMARY KEY,
  email VARCHAR(255) UNIQUE NOT NULL,
  name VARCHAR(100),
  company VARCHAR(100),
  role VARCHAR(100),
  pipeline_shape TEXT,
  monthly_spend VARCHAR(50),
  orchestrator VARCHAR(100),
  notes TEXT,
  design_partner BOOLEAN DEFAULT FALSE,
  ip_address VARCHAR(45),
  request_id VARCHAR(32),
  created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW()),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc', NOW())
);

-- Create an index on email for faster lookups
CREATE INDEX IF NOT EXISTS idx_waitlist_email ON waitlist(email);

-- Create an index on created_at for sorting
CREATE INDEX IF NOT EXISTS idx_waitlist_created_at ON waitlist(created_at DESC);

-- Add RLS (Row Level Security) policies
ALTER TABLE waitlist ENABLE ROW LEVEL SECURITY;

-- Create a policy that allows insert from anon users (for the public waitlist form)
CREATE POLICY "Allow anonymous inserts" ON waitlist
  FOR INSERT WITH CHECK (true);

-- Create a policy that allows authenticated users to view all entries (for admin)
CREATE POLICY "Allow authenticated select" ON waitlist
  FOR SELECT USING (auth.role() = 'authenticated');

-- Create a function to update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = TIMEZONE('utc', NOW());
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create a trigger to automatically update the updated_at column
CREATE TRIGGER update_waitlist_updated_at BEFORE UPDATE
  ON waitlist FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Add a comment to the table
COMMENT ON TABLE waitlist IS 'Stores email waitlist signups for Brevitas Systems';