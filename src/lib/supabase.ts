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
  name?: string | null;
  company?: string | null;
  role?: string | null;
  pipeline_shape?: string | null;
  monthly_spend?: string | null;
  orchestrator?: string | null;
  notes?: string | null;
  design_partner?: boolean;
  created_at?: string;
}