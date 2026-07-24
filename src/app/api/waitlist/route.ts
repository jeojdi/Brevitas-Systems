import { NextRequest, NextResponse } from 'next/server';
import { randomUUID } from 'node:crypto';
import { captureServerEvent } from '@/lib/posthog-server';
import {
  submitWaitlistSignup,
  WaitlistConfigurationError,
  WaitlistUnavailableError,
} from '@/lib/waitlist-server';

interface WaitlistPayload {
  email?: string;
  name?: string;
  company?: string;
  role?: string;
  pipeline_shape?: string;
  monthly_spend?: string;
  orchestrator?: string;
  notes?: string;
  design_partner?: boolean;
  // Legacy fields from older inline form — accepted but mapped.
  use_case?: string;
  source?: string;
}

const EMAIL_PATTERN = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

const FIELD_LIMITS = {
  email: 254,
  name: 100,
  company: 100,
  role: 100,
  pipeline_shape: 2000,
  monthly_spend: 50,
  orchestrator: 100,
  notes: 4000,
  use_case: 4000,
} as const;

class WaitlistValidationError extends Error {}

function trimOrNull(value: unknown, field: keyof typeof FIELD_LIMITS): string | null {
  if (value === undefined || value === null || value === '') return null;
  if (typeof value !== 'string') {
    throw new WaitlistValidationError(`${field} must be a string`);
  }
  const trimmed = value.trim();
  if (trimmed.length > FIELD_LIMITS[field]) {
    throw new WaitlistValidationError(`${field} is too long`);
  }
  return trimmed.length === 0 ? null : trimmed;
}

export async function POST(request: NextRequest) {
  try {
    const parsed: unknown = await request.json();
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new WaitlistValidationError('Request body must be a JSON object');
    }
    const body = parsed as WaitlistPayload;

    const email = trimOrNull(body.email, 'email')?.toLowerCase();
    if (!email || !EMAIL_PATTERN.test(email)) {
      return NextResponse.json(
        { error: 'Invalid email address', success: false },
        { status: 400 }
      );
    }

    // Map legacy `use_case` → `notes` so the inline form keeps working.
    const notes = trimOrNull(body.notes, 'notes') ?? trimOrNull(body.use_case, 'use_case');
    if (body.design_partner !== undefined && typeof body.design_partner !== 'boolean') {
      throw new WaitlistValidationError('design_partner must be a boolean');
    }

    const row = {
      email,
      name: trimOrNull(body.name, 'name'),
      company: trimOrNull(body.company, 'company'),
      role: trimOrNull(body.role, 'role'),
      pipeline_shape: trimOrNull(body.pipeline_shape, 'pipeline_shape'),
      monthly_spend: trimOrNull(body.monthly_spend, 'monthly_spend'),
      orchestrator: trimOrNull(body.orchestrator, 'orchestrator'),
      notes,
      design_partner: body.design_partner ?? false,
    };

    const admission = await submitWaitlistSignup({
      email: row.email,
      name: row.name,
      company: row.company,
      role: row.role,
      pipelineShape: row.pipeline_shape,
      monthlySpend: row.monthly_spend,
      orchestrator: row.orchestrator,
      notes: row.notes,
      designPartner: row.design_partner,
    });

    if (admission.status === 'rate_limited') {
      return NextResponse.json(
        { error: 'Too many waitlist requests. Please try again later.', success: false },
        {
          status: 429,
          headers: {
            'Cache-Control': 'no-store',
            'Retry-After': String(admission.retryAfterSeconds),
          },
        },
      );
    }

    await captureServerEvent({
      // This conversion does not need an email address or other lead details in
      // PostHog. A one-time identifier keeps the event anonymous.
      distinctId: `waitlist:${randomUUID()}`,
      event: 'waitlist_joined',
      properties: {
        source: 'website_waitlist',
        has_company: Boolean(row.company),
        has_orchestrator: Boolean(row.orchestrator),
        requested_design_partnership: row.design_partner,
      },
    });

    return NextResponse.json(
      {
        success: true,
        message: "Thanks — you're on the list. We'll be in touch.",
      },
      { status: 200, headers: { 'Cache-Control': 'no-store' } }
    );
  } catch (error) {
    if (error instanceof WaitlistValidationError || error instanceof SyntaxError) {
      return NextResponse.json(
        {
          error: error instanceof WaitlistValidationError ? error.message : 'Invalid JSON body',
          success: false,
        },
        { status: 400 }
      );
    }
    if (error instanceof WaitlistConfigurationError || error instanceof WaitlistUnavailableError) {
      console.error('Shared waitlist admission is unavailable');
      return NextResponse.json(
        { error: 'Waitlist is temporarily unavailable', success: false },
        { status: 503, headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' } }
      );
    }
    // Never copy exception text or submitted lead fields into logs.
    console.error('Waitlist API request failed');
    return NextResponse.json(
      { error: 'Internal server error', success: false },
      { status: 500, headers: { 'Cache-Control': 'no-store' } }
    );
  }
}
