import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';
import crypto from 'crypto';

// Initialize Supabase client
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
  {
    auth: {
      autoRefreshToken: false,
      persistSession: false
    }
  }
);

// Security: Input validation patterns
const VALIDATION_PATTERNS = {
  email: /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/,
  name: /^[a-zA-Z\s'-]{1,100}$/,
  company: /^[a-zA-Z0-9\s&.,'-]{1,100}$/,
  text: /^[a-zA-Z0-9\s.,!?;:()\-'"@#$%&*+=\/\\{}\[\]]{0,1000}$/
};

// Security: Rate limiting store (in production, use Redis)
const rateLimitStore = new Map();
const RATE_LIMIT_WINDOW = 60000; // 1 minute
const MAX_REQUESTS = 5; // Max 5 submissions per minute per IP

// Security: Check rate limit
function checkRateLimit(ip) {
  const now = Date.now();
  const userRequests = rateLimitStore.get(ip) || [];

  // Clean old requests
  const recentRequests = userRequests.filter(time => now - time < RATE_LIMIT_WINDOW);

  if (recentRequests.length >= MAX_REQUESTS) {
    return false;
  }

  recentRequests.push(now);
  rateLimitStore.set(ip, recentRequests);
  return true;
}

// Security: Sanitize input to prevent XSS and SQL injection
function sanitizeInput(input, type = 'text') {
  if (!input) return '';

  // Remove null bytes
  let sanitized = String(input).replace(/\0/g, '');

  // Trim whitespace
  sanitized = sanitized.trim();

  // HTML entity encoding for special characters
  sanitized = sanitized
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/\//g, '&#x2F;');

  // Additional SQL injection prevention (though Supabase parameterized queries handle this)
  sanitized = sanitized
    .replace(/--/g, '')
    .replace(/;/g, '')
    .replace(/\/\*/g, '')
    .replace(/\*\//g, '');

  // Length limits
  const maxLengths = {
    email: 255,
    name: 100,
    company: 100,
    role: 100,
    orchestrator: 100,
    monthly_spend: 50,
    text: 1000
  };

  if (maxLengths[type]) {
    sanitized = sanitized.substring(0, maxLengths[type]);
  }

  return sanitized;
}

// Security: Validate input format
function validateInput(value, type) {
  if (!value && type !== 'text') return false;

  const pattern = VALIDATION_PATTERNS[type] || VALIDATION_PATTERNS.text;
  return pattern.test(value);
}

// Security: Generate unique request ID for tracking
function generateRequestId() {
  return crypto.randomBytes(16).toString('hex');
}

export async function POST(request) {
  const requestId = generateRequestId();

  try {
    // Get client IP for rate limiting
    const forwarded = request.headers.get('x-forwarded-for');
    const ip = forwarded ? forwarded.split(',')[0].trim() :
               request.headers.get('x-real-ip') ||
               'unknown';

    // Check rate limit
    if (!checkRateLimit(ip)) {
      console.warn(`[${requestId}] Rate limit exceeded for IP: ${ip}`);
      return NextResponse.json(
        { error: 'Too many requests. Please try again later.' },
        { status: 429 }
      );
    }

    // Parse request body
    const body = await request.json();

    // Validate required fields
    if (!body.email) {
      return NextResponse.json(
        { error: 'Email is required' },
        { status: 400 }
      );
    }

    // Sanitize all inputs
    const sanitizedData = {
      email: sanitizeInput(body.email, 'email'),
      name: sanitizeInput(body.name, 'name'),
      company: sanitizeInput(body.company, 'company'),
      role: sanitizeInput(body.role, 'role'),
      pipeline_shape: sanitizeInput(body.pipeline_shape, 'text'),
      monthly_spend: sanitizeInput(body.monthly_spend, 'monthly_spend'),
      orchestrator: sanitizeInput(body.orchestrator, 'orchestrator'),
      notes: sanitizeInput(body.notes, 'text'),
      design_partner: Boolean(body.design_partner),
      created_at: new Date().toISOString(),
      ip_address: ip,
      request_id: requestId
    };

    // Validate email format
    if (!validateInput(sanitizedData.email, 'email')) {
      return NextResponse.json(
        { error: 'Invalid email format' },
        { status: 400 }
      );
    }

    // Validate other fields if provided
    if (sanitizedData.name && !validateInput(sanitizedData.name, 'name')) {
      return NextResponse.json(
        { error: 'Invalid name format' },
        { status: 400 }
      );
    }

    if (sanitizedData.company && !validateInput(sanitizedData.company, 'company')) {
      return NextResponse.json(
        { error: 'Invalid company format' },
        { status: 400 }
      );
    }

    // Check for duplicate email (prevent duplicate submissions)
    const { data: existing, error: checkError } = await supabase
      .from('waitlist')
      .select('email')
      .eq('email', sanitizedData.email)
      .single();

    if (existing) {
      console.log(`[${requestId}] Duplicate submission for email: ${sanitizedData.email}`);
      // Return success to prevent email enumeration
      return NextResponse.json(
        { success: true, message: 'Thank you for your interest!' },
        { status: 200 }
      );
    }

    // Insert into database using parameterized query (prevents SQL injection)
    const { data, error } = await supabase
      .from('waitlist')
      .insert([sanitizedData])
      .select();

    if (error) {
      console.error(`[${requestId}] Database error:`, error);
      return NextResponse.json(
        { error: 'Failed to submit form. Please try again.' },
        { status: 500 }
      );
    }

    console.log(`[${requestId}] Waitlist submission successful for email: ${sanitizedData.email}`);

    return NextResponse.json(
      {
        success: true,
        message: 'Thank you for joining the waitlist!',
        requestId: requestId
      },
      {
        status: 200,
        headers: {
          'X-Request-Id': requestId,
          'X-Content-Type-Options': 'nosniff',
          'X-Frame-Options': 'DENY',
          'X-XSS-Protection': '1; mode=block',
          'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
          'Content-Security-Policy': "default-src 'self'"
        }
      }
    );

  } catch (error) {
    console.error(`[${requestId}] Unexpected error:`, error);
    return NextResponse.json(
      { error: 'An unexpected error occurred' },
      { status: 500 }
    );
  }
}

// Security: Only allow POST method
export async function GET() {
  return NextResponse.json(
    { error: 'Method not allowed' },
    { status: 405 }
  );
}

export async function PUT() {
  return NextResponse.json(
    { error: 'Method not allowed' },
    { status: 405 }
  );
}

export async function DELETE() {
  return NextResponse.json(
    { error: 'Method not allowed' },
    { status: 405 }
  );
}