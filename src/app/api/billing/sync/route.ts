import { billingConfig } from '@/lib/billing/config';
import { recoveryBearerAuthorized } from '@/lib/billing/recovery-auth.mjs';
import { billingDatabase } from '@/lib/billing/supabase';

export const runtime = 'nodejs';
export const maxDuration = 10;

function recoveryAuthorized(request: Request): boolean {
  return recoveryBearerAuthorized(
    request.headers.get('authorization'),
    billingConfig().recoverySecret,
  );
}

export async function POST(request: Request) {
  if (!recoveryAuthorized(request)) return Response.json({ error: 'Unauthorized' }, { status: 401 });
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
  const { data: resolved, error } = await billingDatabase().rpc(
    'manually_resolve_billing_ledger_entry',
    { p_entry_id: entryId, p_resolution: resolution, p_note: note },
  );
  if (error) throw error;
  if (!resolved) {
    return Response.json({ error: 'Ledger entry is not eligible for manual recovery' }, { status: 409 });
  }
  return Response.json(
    { resolved: true, entry_id: entryId, resolution, manual_only: true },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}
