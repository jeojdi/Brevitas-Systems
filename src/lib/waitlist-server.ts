import 'server-only';

import { createClient } from '@supabase/supabase-js';
import { parseWaitlistAdmission } from '@/lib/waitlist-admission.mjs';

export interface WaitlistSignup {
  email: string;
  name: string | null;
  company: string | null;
  role: string | null;
  pipelineShape: string | null;
  monthlySpend: string | null;
  orchestrator: string | null;
  notes: string | null;
  designPartner: boolean;
}

export class WaitlistConfigurationError extends Error {
  constructor() {
    super('Waitlist persistence is not configured');
    this.name = 'WaitlistConfigurationError';
  }
}

export class WaitlistUnavailableError extends Error {
  constructor() {
    super('Shared waitlist admission is unavailable');
    this.name = 'WaitlistUnavailableError';
  }
}

export type WaitlistSubmissionResult =
  | { status: 'accepted' }
  | { status: 'rate_limited'; retryAfterSeconds: number };

function waitlistDatabase() {
  const url = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || '';
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
  if (!url || !serviceKey) throw new WaitlistConfigurationError();

  return createClient(url, serviceKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
}

export async function submitWaitlistSignup(
  signup: WaitlistSignup,
): Promise<WaitlistSubmissionResult> {
  const { data, error } = await waitlistDatabase().rpc('submit_waitlist_signup', {
    p_email: signup.email,
    p_name: signup.name,
    p_company: signup.company,
    p_role: signup.role,
    p_pipeline_shape: signup.pipelineShape,
    p_monthly_spend: signup.monthlySpend,
    p_orchestrator: signup.orchestrator,
    p_notes: signup.notes,
    p_design_partner: signup.designPartner,
  });
  // The old boolean RPC and malformed/failed responses must never bypass the
  // shared limiter during a partial migration or dependency outage.
  if (error) {
    throw new WaitlistUnavailableError();
  }
  try {
    return parseWaitlistAdmission(data);
  } catch {
    throw new WaitlistUnavailableError();
  }
}
