import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error('Missing Supabase environment variables');
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey);

// Type definitions for our database tables
export interface WaitlistEntry {
  id?: number;
  email: string;
  company?: string;
  role?: string;
  use_case?: string;
  name?: string;
  source?: string;
  created_at?: string;
}