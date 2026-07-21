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

export async function POST(request: Request) {
  const maintenanceResponse = billingMaintenanceResponse();
  if (maintenanceResponse) return maintenanceResponse;

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
