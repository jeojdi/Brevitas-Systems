import 'server-only';

import { createClient } from '@supabase/supabase-js';
import { parseWaitlistAdmission } from '@/lib/waitlist-admission.mjs';
import {
  parseWaitlistNetworkBudget,
  WAITLIST_NETWORK_LIMIT,
  WAITLIST_NETWORK_WINDOW_SECONDS,
} from '@/lib/waitlist-network.mjs';

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

type WaitlistDatabase = ReturnType<typeof waitlistDatabase>;

/**
 * The network budget is an ADDITIONAL window in front of the global one, so
 * every way it can fail degrades to exactly the pre-existing behaviour. It is
 * announced once per process and never again: the waitlist is a signup form,
 * and refusing signups because a rate-limit migration has not been hand-applied
 * yet would be worse than the abuse the bucket exists to stop.
 */
let networkBudgetDegradeLogged = false;

function logNetworkBudgetDegradeOnce(reason: string): void {
  if (networkBudgetDegradeLogged) return;
  networkBudgetDegradeLogged = true;
  // `reason` is an error code or a fixed token — never an address, never the
  // derived key, never a submitted lead field.
  console.warn(
    'Waitlist network budget unavailable; charging the global window only',
    reason,
  );
}

/**
 * Charge public.consume_waitlist_network_budget (service_role only) BEFORE the
 * global window inside submit_waitlist_signup.
 *
 * @returns a rate-limit result when the network is over budget, or null to
 *   continue to the global window — including every fail-open path.
 */
async function chargeWaitlistNetworkBudget(
  database: WaitlistDatabase,
  networkKey: string | null,
): Promise<WaitlistSubmissionResult | null> {
  if (!networkKey) {
    // No salt configured, or no resolvable client address. Not an error state:
    // the global window is still ahead of us.
    logNetworkBudgetDegradeOnce('no-network-key');
    return null;
  }
  let data: unknown;
  let error: { code?: unknown } | null = null;
  try {
    ({ data, error } = await database.rpc('consume_waitlist_network_budget', {
      p_network_key: networkKey,
      p_limit: WAITLIST_NETWORK_LIMIT,
      p_window_seconds: WAITLIST_NETWORK_WINDOW_SECONDS,
    }));
  } catch {
    logNetworkBudgetDegradeOnce('transport');
    return null;
  }
  if (error) {
    // PGRST202/42883 mean the RPC has not reached this database yet (Vercel
    // deploys on push; wyfz migrations are applied by hand, one file at a
    // time), 42501 means its grant has not. Any other code is treated the same
    // way here on purpose — see the fail-open note above.
    logNetworkBudgetDegradeOnce(typeof error.code === 'string' ? error.code : 'no-code');
    return null;
  }
  try {
    const verdict = parseWaitlistNetworkBudget(data);
    return verdict.status === 'rate_limited'
      ? { status: 'rate_limited', retryAfterSeconds: verdict.retryAfterSeconds }
      : null;
  } catch {
    logNetworkBudgetDegradeOnce('unparseable');
    return null;
  }
}

export async function submitWaitlistSignup(
  signup: WaitlistSignup,
  networkKey: string | null = null,
): Promise<WaitlistSubmissionResult> {
  const database = waitlistDatabase();
  const overBudget = await chargeWaitlistNetworkBudget(database, networkKey);
  if (overBudget) return overBudget;

  const { data, error } = await database.rpc('submit_waitlist_signup', {
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
