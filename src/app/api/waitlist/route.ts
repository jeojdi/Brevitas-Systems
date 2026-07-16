import { NextRequest, NextResponse } from 'next/server';
import { randomUUID } from 'node:crypto';
import { supabase } from '@/lib/supabase';
import { withRateLimit, RATE_LIMITS } from '@/lib/rate-limiter';
import { captureServerEvent } from '@/lib/posthog-server';

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

function trimOrNull(value: string | undefined | null): string | null {
  if (!value) return null;
  const trimmed = String(value).trim();
  return trimmed.length === 0 ? null : trimmed;
}

export async function POST(request: NextRequest) {
  return withRateLimit(request, async (req) => {
    try {
      const body = (await req.json()) as WaitlistPayload;

      const email = trimOrNull(body.email)?.toLowerCase();
      if (!email || !EMAIL_PATTERN.test(email)) {
        return NextResponse.json(
          { error: 'Invalid email address', success: false },
          { status: 400 }
        );
      }

      if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
        console.warn('Supabase not configured, falling back to console logging');
        console.log('New waitlist signup (demo mode):', { email, ...body });
        return NextResponse.json(
          { success: true, message: 'Successfully joined the waitlist (demo mode)' },
          { status: 200 }
        );
      }

      // Map legacy `use_case` → `notes` so the inline form keeps working.
      const notes = trimOrNull(body.notes) ?? trimOrNull(body.use_case);

      const row = {
        email,
        name: trimOrNull(body.name),
        company: trimOrNull(body.company),
        role: trimOrNull(body.role),
        pipeline_shape: trimOrNull(body.pipeline_shape),
        monthly_spend: trimOrNull(body.monthly_spend),
        orchestrator: trimOrNull(body.orchestrator),
        notes,
        design_partner: Boolean(body.design_partner),
      };

      const { error } = await supabase
        .from('waitlist')
        .insert([row]);

      if (error) {
        if (error.code === '23505') {
          // Duplicate email — return success to avoid leaking enumeration.
          return NextResponse.json(
            { success: true, message: "You're already on the list — thanks!" },
            { status: 200 }
          );
        }

        console.error('Supabase insert error:', error);
        return NextResponse.json(
          { error: 'Failed to join waitlist. Please try again.', success: false },
          { status: 500 }
        );
      }

      console.log('New waitlist signup saved:', { email });

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
        { status: 200 }
      );
    } catch (error) {
      console.error('Waitlist API error:', error);
      return NextResponse.json(
        { error: 'Internal server error', success: false },
        { status: 500 }
      );
    }
  }, RATE_LIMITS.formSubmission);
}
