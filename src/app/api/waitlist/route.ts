import { NextRequest, NextResponse } from 'next/server';
import { supabase } from '@/lib/supabase';
import { withRateLimit, RATE_LIMITS } from '@/lib/rate-limiter';

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

      const { data, error } = await supabase
        .from('waitlist')
        .insert([row])
        .select()
        .single();

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

      console.log('New waitlist signup saved:', { id: data?.id, email: data?.email });

      return NextResponse.json(
        {
          success: true,
          message: "Thanks — you're on the list. We'll be in touch.",
          data: { id: data?.id, email: data?.email },
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

export async function GET(request: NextRequest) {
  return withRateLimit(request, async (req) => {
    try {
      const email = trimOrNull(req.nextUrl.searchParams.get('email'))?.toLowerCase();
      if (!email) {
        return NextResponse.json(
          { error: 'Email parameter is required' },
          { status: 400 }
        );
      }

      if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
        return NextResponse.json({ exists: false }, { status: 200 });
      }

      const { data, error } = await supabase
        .from('waitlist')
        .select('email')
        .eq('email', email)
        .single();

      if (error && error.code !== 'PGRST116') {
        console.error('Supabase select error:', error);
        return NextResponse.json(
          { error: 'Failed to check waitlist status' },
          { status: 500 }
        );
      }

      return NextResponse.json({ exists: !!data }, { status: 200 });
    } catch (error) {
      console.error('Waitlist check error:', error);
      return NextResponse.json(
        { error: 'Internal server error' },
        { status: 500 }
      );
    }
  }, RATE_LIMITS.api);
}
