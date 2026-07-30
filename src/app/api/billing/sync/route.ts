import { billingConfig } from '@/lib/billing/config';
import { billingMaintenanceResponse } from '@/lib/billing/maintenance-gate.mjs';
import {
  recoverySecretAuthorized,
  recoverySecretIsStrong,
} from '@/lib/billing/recovery-auth.mjs';
import {
  authenticatedBillingUser,
  authorizeActiveBillingCompany,
  BillingRecoveryAdmissionError,
  consumeBillingRecoveryAttempt,
  manuallyResolveBillingLedgerEntry,
} from '@/lib/billing/supabase';

export const runtime = 'nodejs';
export const maxDuration = 10;

const REQUEST_ID = /^[A-Za-z0-9._:-]{8,128}$/;

/**
 * PostgREST/Postgres codes that mean a dependency of this handler is not
 * reachable in THIS database (absent function or table, or a missing execute
 * grant) rather than that a request was bad. Same set, same reasoning, as
 * src/app/api/billing/status/route.ts:76-88 — production Postgres has no
 * migration ledger and migrations are hand-applied one file at a time while
 * Vercel deploys on push, so schema drift is a normal transient state of a
 * rollout, not a fault.
 */
const DEPENDENCY_UNAVAILABLE_CODES = new Set([
  'PGRST202', // function absent from the PostgREST schema cache
  '42883', // undefined_function
  '42P01', // undefined_table
  '42501', // insufficient_privilege (execute grant not applied)
]);

function dependencyUnavailable(error: unknown): boolean {
  const candidate = error as { code?: unknown; message?: unknown } | null;
  if (typeof candidate?.code === 'string' && DEPENDENCY_UNAVAILABLE_CODES.has(candidate.code)) {
    return true;
  }
  // Older PostgREST schema-cache misses surface without a stable code.
  return typeof candidate?.message === 'string' &&
    /could not find the function|does not exist|permission denied/i.test(candidate.message);
}

/**
 * The retryable degrade, byte-identical to the admission branch below.
 *
 * The handler had NO outer catch: `authorizeActiveBillingCompany` and
 * `manuallyResolveBillingLedgerEntry` both rethrow the raw PostgREST error, so
 * a missing RPC or a missing grant escaped the route entirely and the framework
 * served an uncontrolled 500 — with no `Cache-Control: no-store` (I13 requires
 * it on every non-2xx here) and no `Retry-After`, so an operator working a
 * stuck ledger row mid-rollout could not tell "retry in a moment" from "this
 * entry is unresolvable".
 *
 * There is deliberately no logging: this route must not write to the console at
 * all — an operator-supplied note and a request id pass through it.
 */
function recoveryUnavailableResponse(): Response {
  return Response.json(
    { error: 'Billing recovery is temporarily unavailable' },
    {
      status: 503,
      headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' },
    },
  );
}

export async function POST(request: Request) {
  const maintenanceResponse = billingMaintenanceResponse();
  if (maintenanceResponse) return maintenanceResponse;
  try {
    return await handleManualRecovery(request);
  } catch (error) {
    // ONLY schema drift degrades, exactly as in
    // src/app/api/billing/status/route.ts:143. Every other error still
    // propagates unchanged so a genuine fault stays loud and visible instead of
    // being masked as a retryable 503 — a property
    // tests/billing_sync_input_ladder.test.mjs:268-272 pins deliberately.
    if (!dependencyUnavailable(error)) throw error;
    return recoveryUnavailableResponse();
  }
}

async function handleManualRecovery(request: Request): Promise<Response> {
  const user = await authenticatedBillingUser(request);
  if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
  const authorization = await authorizeActiveBillingCompany(user.id);
  if (!authorization.ok || !authorization.organizationId) {
    return Response.json(
      { error: 'Billing permission is required for the active company' },
      { status: 403 },
    );
  }

  let admission: Awaited<ReturnType<typeof consumeBillingRecoveryAttempt>>;
  try {
    admission = await consumeBillingRecoveryAttempt(
      user.id,
      authorization.organizationId,
    );
  } catch (error) {
    if (!(error instanceof BillingRecoveryAdmissionError)) throw error;
    return Response.json(
      { error: 'Billing recovery is temporarily unavailable' },
      {
        status: 503,
        headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' },
      },
    );
  }
  if (admission.status === 'rate_limited') {
    return Response.json(
      { error: 'Too many billing recovery attempts' },
      {
        status: 429,
        headers: {
          'Cache-Control': 'no-store',
          'Retry-After': String(admission.retryAfterSeconds),
        },
      },
    );
  }

  // The recovery header is a second factor, never the caller identity. Do not
  // read or compare it until Supabase has authenticated the actor, canonical
  // company authorization has succeeded, and the shared attempt was admitted.
  const recoverySecret = billingConfig().recoverySecret;
  if (!recoverySecretIsStrong(recoverySecret)) {
    return Response.json(
      { error: 'Billing recovery is temporarily unavailable' },
      {
        status: 503,
        headers: { 'Cache-Control': 'no-store', 'Retry-After': '300' },
      },
    );
  }
  if (!recoverySecretAuthorized(
    request.headers.get('x-billing-recovery-secret'),
    recoverySecret,
  )) {
    return Response.json(
      { error: 'Recovery second factor is required' },
      { status: 401, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) {
    return Response.json({ error: 'Content-Type must be application/json' }, { status: 415 });
  }
  const contentLength = Number(request.headers.get('content-length') || 0);
  if (contentLength > 4096) {
    return Response.json({ error: 'Request body is too large' }, { status: 413 });
  }
  let body: unknown;
  try {
    const rawBody = await request.text();
    if (Buffer.byteLength(rawBody, 'utf8') > 4096) {
      return Response.json({ error: 'Request body is too large' }, { status: 413 });
    }
    body = JSON.parse(rawBody);
  } catch {
    return Response.json({ error: 'Invalid JSON body' }, { status: 400 });
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return Response.json({ error: 'Invalid manual recovery request' }, { status: 400 });
  }
  const candidate = body as { entry_id?: unknown; resolution?: unknown; note?: unknown };
  const entryId = Number(candidate.entry_id);
  const resolution = candidate.resolution;
  const note = typeof candidate.note === 'string' ? candidate.note.trim() : '';
  if (
    !Number.isSafeInteger(entryId) || entryId <= 0 ||
    !['reported', 'dead', 'pending'].includes(String(resolution)) ||
    note.length < 12 || note.length > 480
  ) {
    return Response.json({ error: 'Invalid manual recovery request' }, { status: 400 });
  }
  const incomingRequestId = request.headers.get('x-request-id') || '';
  const requestId = REQUEST_ID.test(incomingRequestId) ? incomingRequestId : crypto.randomUUID();
  const result = await manuallyResolveBillingLedgerEntry({
    actorUserId: user.id,
    expectedOrganizationId: authorization.organizationId,
    entryId,
    resolution: String(resolution),
    note,
    requestId,
  });
  if (!result.ok) {
    const authorizationChanged = ['forbidden', 'active_company_changed'].includes(result.code);
    return Response.json(
      {
        error: authorizationChanged
          ? 'Billing permission is required for the active company'
          : 'Ledger entry is not eligible for manual recovery',
      },
      {
        status: authorizationChanged ? 403 : 409,
        headers: { 'Cache-Control': 'no-store', 'X-Request-ID': requestId },
      },
    );
  }
  return Response.json(
    {
      resolved: true,
      entry_id: entryId,
      resolution,
      audit_id: result.auditId,
      manual_only: true,
    },
    { headers: { 'Cache-Control': 'no-store', 'X-Request-ID': requestId } },
  );
}
