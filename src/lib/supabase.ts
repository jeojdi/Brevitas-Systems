import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

export type SupabasePublicKeyKind = 'anon' | 'publishable' | 'service-secret' | 'invalid' | 'missing';

function decodeJwtRole(value: string): string {
  const parts = value.split('.');
  if (parts.length !== 3 || typeof globalThis.atob !== 'function') return '';
  try {
    const encoded = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = globalThis.atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '='));
    const parsed = JSON.parse(payload) as { role?: unknown };
    return typeof parsed.role === 'string' ? parsed.role : '';
  } catch {
    return '';
  }
}

export function supabasePublicKeyKind(value: string | undefined): SupabasePublicKeyKind {
  if (!value) return 'missing';
  if (value.startsWith('sb_publishable_')) return 'publishable';
  if (value.startsWith('sb_secret_')) return 'service-secret';
  const role = decodeJwtRole(value);
  if (role === 'anon') return 'anon';
  if (role === 'service_role') return 'service-secret';
  return 'invalid';
}

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error('Missing Supabase environment variables');
}

export const supabaseCredentialKind = supabasePublicKeyKind(supabaseAnonKey);
if (!['anon', 'publishable'].includes(supabaseCredentialKind)) {
  throw new Error('Unsafe Supabase browser credential configuration');
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
