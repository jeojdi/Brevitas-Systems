import { NextRequest, NextResponse } from 'next/server';
import { supabase } from '@/lib/supabase';
import { withRateLimit, RATE_LIMITS } from '@/lib/rate-limiter';

export async function POST(request: NextRequest) {
  // Apply strict rate limiting for form submissions (3 per minute per IP)
  return withRateLimit(request, async (req) => {
    try {
      const body = await req.json();
    const { email, company, role, use_case, name, source } = body;

    // Validate email
    if (!email || !email.includes('@')) {
      return NextResponse.json(
        { error: 'Invalid email address' },
        { status: 400 }
      );
    }

    // Check if Supabase is configured
    if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
      console.warn('Supabase not configured, falling back to console logging');
      console.log('New waitlist signup:', {
        email,
        company,
        role,
        use_case,
        name,
        source,
        timestamp: new Date().toISOString(),
      });

      return NextResponse.json(
        {
          success: true,
          message: 'Successfully joined the waitlist (demo mode)',
        },
        { status: 200 }
      );
    }

    // Save to Supabase
    const { data, error } = await supabase
      .from('waitlist')
      .insert([
        {
          email: email.toLowerCase().trim(),
          company: company || null,
          role: role || null,
          use_case: use_case || null,
          name: name || null,
          source: source || null,
        }
      ])
      .select()
      .single();

    if (error) {
      // Check if it's a duplicate email error
      if (error.code === '23505') {
        return NextResponse.json(
          {
            error: 'This email is already on the waitlist',
            success: false
          },
          { status: 409 }
        );
      }

      console.error('Supabase error:', error);
      return NextResponse.json(
        {
          error: 'Failed to join waitlist. Please try again.',
          success: false
        },
        { status: 500 }
      );
    }

    console.log('New waitlist signup saved:', data);

    // Here you could also:
    // - Send confirmation email
    // - Notify team via Slack/Discord
    // - Add to CRM/mailing list

    return NextResponse.json(
      {
        success: true,
        message: 'Successfully joined the waitlist! We\'ll be in touch soon.',
        data: {
          id: data.id,
          email: data.email
        }
      },
      { status: 200 }
    );
  } catch (error) {
    console.error('Waitlist API error:', error);
    return NextResponse.json(
      {
        error: 'Internal server error',
        success: false
      },
      { status: 500 }
    );
  }
  }, RATE_LIMITS.formSubmission); // Use strict rate limit for form submissions
}

// GET endpoint to check if an email is already on the waitlist (optional)
export async function GET(request: NextRequest) {
  // Apply medium rate limiting for GET requests (30 per minute per IP)
  return withRateLimit(request, async (req) => {
    try {
      const searchParams = req.nextUrl.searchParams;
    const email = searchParams.get('email');

    if (!email) {
      return NextResponse.json(
        { error: 'Email parameter is required' },
        { status: 400 }
      );
    }

    // Check if Supabase is configured
    if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
      return NextResponse.json(
        { exists: false },
        { status: 200 }
      );
    }

    const { data, error } = await supabase
      .from('waitlist')
      .select('email')
      .eq('email', email.toLowerCase().trim())
      .single();

    if (error && error.code !== 'PGRST116') { // PGRST116 = no rows returned
      console.error('Supabase error:', error);
      return NextResponse.json(
        { error: 'Failed to check waitlist status' },
        { status: 500 }
      );
    }

    return NextResponse.json(
      { exists: !!data },
      { status: 200 }
    );
  } catch (error) {
    console.error('Waitlist check error:', error);
    return NextResponse.json(
      { error: 'Internal server error' },
      { status: 500 }
    );
  }
  }, RATE_LIMITS.api); // Use standard API rate limit for GET requests
}