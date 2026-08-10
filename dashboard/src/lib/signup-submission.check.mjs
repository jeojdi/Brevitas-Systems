import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeAuthEmail, PASSWORD_MISMATCH_MESSAGE } from './auth-credentials.js'
import {
  DUPLICATE_SIGNUP_NOTICE,
  SIGNUP_CONFIRMATION_NOTICE,
  SIGNUP_TRACKER_MAX_ENTRIES,
  createSignupTracker,
  signupFailureReason,
} from './signup-submission.js'
import { EMAIL_DELIVERY_FAILED_MESSAGE } from './supabase.js'

const sentenceCount = copy => copy.split(/[.!?]+\s+|[.!?]+$/).filter(Boolean).length

test('post-signup copy tells the user to confirm by email, not to wait for an invite', () => {
  assert.match(SIGNUP_CONFIRMATION_NOTICE, /confirmation link/i)
  assert.match(SIGNUP_CONFIRMATION_NOTICE, /click it/i)
  // With auto-confirm on, this notice only shows when GoTrue withheld a session,
  // which usually means the mail really is in transit. Setting the delivery
  // expectation up front is what stops the "nothing arrived" support loop.
  assert.match(SIGNUP_CONFIRMATION_NOTICE, /minute or two/i)
  assert.match(SIGNUP_CONFIRMATION_NOTICE, /spam/i)
  assert.match(SIGNUP_CONFIRMATION_NOTICE, /"Forgot password"/)
  assert.equal(sentenceCount(SIGNUP_CONFIRMATION_NOTICE), 3)
  assert.doesNotMatch(SIGNUP_CONFIRMATION_NOTICE, /—/)

  // The waitlist framing is what drove the duplicate signup in the first place.
  assert.doesNotMatch(SIGNUP_CONFIRMATION_NOTICE, /waitlist|invite|reach out|request was received/i)
})

test('duplicate-signup copy steers to reset or resend and never claims a saved password', () => {
  assert.match(DUPLICATE_SIGNUP_NOTICE, /already submitted/i)
  assert.match(DUPLICATE_SIGNUP_NOTICE, /does not save a new password/i)
  assert.match(DUPLICATE_SIGNUP_NOTICE, /resend/i)
  assert.match(DUPLICATE_SIGNUP_NOTICE, /"Forgot password"/)
  assert.doesNotMatch(DUPLICATE_SIGNUP_NOTICE, /—/)

  // Claiming the second password was stored is the exact lie that locked the user out.
  assert.doesNotMatch(DUPLICATE_SIGNUP_NOTICE, /password (was|has been) (saved|updated|changed)/i)
  assert.doesNotMatch(DUPLICATE_SIGNUP_NOTICE, /new password (was|has been) (saved|stored)/i)
})

test('a fresh tracker reports nothing attempted', () => {
  const tracker = createSignupTracker()
  assert.equal(tracker.hasAttempted('user@example.com'), false)
  tracker.record('user@example.com')
  assert.equal(tracker.hasAttempted('user@example.com'), true)
  assert.equal(tracker.hasAttempted('other@example.com'), false)
})

test('email comparison folds case and surrounding whitespace', () => {
  const tracker = createSignupTracker()
  tracker.record('User@Example.COM')

  for (const variant of [
    'user@example.com',
    'USER@EXAMPLE.COM',
    '  user@example.com  ',
    '\tuser@example.com\n',
  ]) {
    assert.equal(tracker.hasAttempted(variant), true, variant)
  }

  assert.equal(tracker.hasAttempted('user@example.co'), false)
  assert.equal(tracker.hasAttempted('userexample.com'), false)
})

test('internal whitespace is a different address, matching what Supabase is sent', () => {
  // The tracker must agree with normalizeAuthEmail, which the form uses for the
  // actual signUp call. Collapsing internal whitespace here would let the tracker
  // suppress a signup for an address GoTrue considers distinct.
  const tracker = createSignupTracker()
  tracker.record('user@example.com')

  assert.equal(tracker.hasAttempted('user @ example.com'), false)
  assert.equal(normalizeAuthEmail('user @ example.com'), 'user @ example.com')
})

test('a whitespace-only or non-string email is neither recorded nor matched', () => {
  const tracker = createSignupTracker()
  for (const value of ['', '   ', '\n\t', null, undefined, 42, {}, [], Symbol('user@example.com')]) {
    tracker.record(value)
    assert.equal(tracker.hasAttempted(value), false, String(typeof value))
  }
  // A blank submission must not make an unrelated real address look attempted.
  assert.equal(tracker.hasAttempted('user@example.com'), false)
})

test('the store is bounded and evicts the oldest recorded address', () => {
  const tracker = createSignupTracker(3)
  tracker.record('a@example.com')
  tracker.record('b@example.com')
  tracker.record('c@example.com')
  assert.equal(tracker.hasAttempted('a@example.com'), true)

  tracker.record('d@example.com')
  assert.equal(tracker.hasAttempted('a@example.com'), false)
  for (const email of ['b@example.com', 'c@example.com', 'd@example.com']) {
    assert.equal(tracker.hasAttempted(email), true, email)
  }
})

test('re-recording an address refreshes it rather than duplicating it', () => {
  const tracker = createSignupTracker(2)
  tracker.record('a@example.com')
  tracker.record('b@example.com')
  tracker.record('  A@EXAMPLE.COM  ')
  tracker.record('c@example.com')

  // 'a' was refreshed by the case/whitespace variant, so 'b' is the oldest.
  assert.equal(tracker.hasAttempted('b@example.com'), false)
  assert.equal(tracker.hasAttempted('a@example.com'), true)
  assert.equal(tracker.hasAttempted('c@example.com'), true)
})

test('the default bound holds at 64 entries', () => {
  assert.equal(SIGNUP_TRACKER_MAX_ENTRIES, 64)
  const tracker = createSignupTracker()
  for (let index = 0; index < SIGNUP_TRACKER_MAX_ENTRIES; index += 1) {
    tracker.record(`user${index}@example.com`)
  }
  assert.equal(tracker.hasAttempted('user0@example.com'), true)

  tracker.record('overflow@example.com')
  assert.equal(tracker.hasAttempted('user0@example.com'), false)
  assert.equal(tracker.hasAttempted('user1@example.com'), true)
  assert.equal(tracker.hasAttempted('overflow@example.com'), true)
})

test('reset clears every recorded address', () => {
  const tracker = createSignupTracker()
  tracker.record('user@example.com')
  tracker.record('other@example.com')
  tracker.reset()
  assert.equal(tracker.hasAttempted('user@example.com'), false)
  assert.equal(tracker.hasAttempted('other@example.com'), false)

  tracker.record('user@example.com')
  assert.equal(tracker.hasAttempted('user@example.com'), true)
})

test('trackers do not share state', () => {
  const first = createSignupTracker()
  const second = createSignupTracker()
  first.record('user@example.com')
  assert.equal(second.hasAttempted('user@example.com'), false)
  second.reset()
  assert.equal(first.hasAttempted('user@example.com'), true)
})

test('the mail-delivery failure the friendly copy maps to gets its own bucket', () => {
  // The caller passes this in because the pattern lives in supabase.js, next to the
  // copy. Pinning it against the real exported constant keeps the two in step: if the
  // wording moves and Auth.jsx's `===` stops matching, this fails rather than silently
  // relabelling every undeliverable address as a generic auth error.
  assert.equal(
    signupFailureReason(EMAIL_DELIVERY_FAILED_MESSAGE, { emailDeliveryFailed: true }),
    'email_delivery',
  )
  assert.equal(signupFailureReason('anything at all', { emailDeliveryFailed: true }), 'email_delivery')
})

test('a mismatch is recognised by identity, not by guessing at the word "password"', () => {
  assert.equal(signupFailureReason(PASSWORD_MISMATCH_MESSAGE), 'password_mismatch')
  // Still a password problem, but a different one, so it must not claim mismatch.
  assert.equal(signupFailureReason('Password should be at least 6 characters'), 'weak_password')
})

test('provider and transport failures are separated', () => {
  assert.equal(signupFailureReason('Email rate limit exceeded'), 'rate_limited')
  assert.equal(signupFailureReason('User already registered'), 'duplicate_email')
  assert.equal(signupFailureReason('Failed to fetch'), 'network')
  assert.equal(signupFailureReason('NetworkError when attempting to fetch resource.'), 'network')
  assert.equal(signupFailureReason('Load failed'), 'network')
  assert.equal(signupFailureReason('Something went wrong on our end.'), 'auth_error')
})

test('an absent or unusable message still yields a bucket, never a throw', () => {
  for (const value of [undefined, null, '', '   ', {}, 42]) {
    const reason = signupFailureReason(value)
    assert.equal(typeof reason, 'string')
    assert.ok(reason.length > 0)
  }
  assert.equal(signupFailureReason(null), 'unknown')
})

test('no reason bucket can leak an address, a password, or raw provider text', () => {
  // The whole point of a fixed vocabulary: whatever GoTrue says, the property we send
  // to PostHog is drawn from this closed set and never echoes the input.
  const vocabulary = new Set([
    'email_delivery', 'password_mismatch', 'weak_password', 'rate_limited',
    'duplicate_email', 'network', 'auth_error', 'unknown',
  ])
  const hostile = [
    'Error sending confirmation email to ada@example.com',
    'password "hunter2" is too weak',
    PASSWORD_MISMATCH_MESSAGE,
    'Email rate limit exceeded for ada@example.com',
  ]
  for (const message of hostile) {
    const reason = signupFailureReason(message)
    assert.ok(vocabulary.has(reason), `${reason} is not in the closed vocabulary`)
    assert.ok(!reason.includes('@'), 'a reason must never carry an address')
    assert.ok(!reason.includes('hunter2'), 'a reason must never carry a password')
  }
})
