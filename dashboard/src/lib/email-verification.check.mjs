import assert from 'node:assert/strict'
import test from 'node:test'

import {
  EMAIL_VERIFIED_PARAM,
  PENDING_VERIFICATION_METADATA,
  emailVerificationRedirect,
  isEmailVerificationReturn,
  markEmailVerified,
  needsEmailVerification,
  sendEmailVerificationLink,
  urlWithoutVerificationMarker,
} from './email-verification.js'

test('signup metadata marks the account as unproven', () => {
  assert.equal(PENDING_VERIFICATION_METADATA.needs_email_verification, true)
})

test('only accounts created by the deferred flow are asked to verify', () => {
  assert.equal(needsEmailVerification({ user_metadata: { needs_email_verification: true } }), true)

  for (const user of [
    null,
    undefined,
    {},
    { user_metadata: null },
    { user_metadata: {} },
    // Pre-existing accounts confirmed the old way carry no flag.
    { user_metadata: { accepted_terms_at: '2026-07-01T00:00:00.000Z' } },
    // A cleared flag must not be read as "still pending".
    { user_metadata: { needs_email_verification: false } },
    // Truthy-but-not-true values are metadata we did not write.
    { user_metadata: { needs_email_verification: 'true' } },
    { user_metadata: { needs_email_verification: 1 } },
  ]) {
    assert.equal(needsEmailVerification(user), false, JSON.stringify(user))
  }
})

test('a recorded verification timestamp wins over a stale pending flag', () => {
  // updateUser merges metadata, so a client that wrote the timestamp without
  // clearing the flag must still count as verified.
  assert.equal(needsEmailVerification({
    user_metadata: { needs_email_verification: true, email_verified_at: '2026-07-29T00:00:00.000Z' },
  }), false)
  assert.equal(needsEmailVerification({
    user_metadata: { needs_email_verification: true, email_verified_at: '' },
  }), true)
})

test('the emailed link always returns to the dashboard, marked', () => {
  assert.equal(
    emailVerificationRedirect({ origin: 'https://brevitassystems.com', pathname: '/login/personal' }),
    `https://brevitassystems.com/dashboard?${EMAIL_VERIFIED_PARAM}=1`,
  )
  assert.equal(
    emailVerificationRedirect({ origin: 'http://localhost:5173' }),
    `http://localhost:5173/dashboard?${EMAIL_VERIFIED_PARAM}=1`,
  )
})

test('the return trip is recognized only when the marker is present', () => {
  assert.equal(isEmailVerificationReturn('?email_verified=1'), true)
  assert.equal(isEmailVerificationReturn('?preview=dashboard&email_verified=1'), true)
  for (const search of ['', '?', '?email_verified=0', '?email_verified=', '?verified=1', null, undefined]) {
    assert.equal(isEmailVerificationReturn(search), false, String(search))
  }
})

test('the marker is stripped without disturbing the rest of the URL', () => {
  assert.equal(
    urlWithoutVerificationMarker({ pathname: '/dashboard', search: '?email_verified=1', hash: '' }),
    '/dashboard',
  )
  assert.equal(
    urlWithoutVerificationMarker({ pathname: '/dashboard', search: '?email_verified=1&tab=Savings', hash: '#top' }),
    '/dashboard?tab=Savings#top',
  )
  assert.equal(urlWithoutVerificationMarker({}), '/')
})

test('sending a link never creates a second account and comes back to the dashboard', async () => {
  const calls = []
  await sendEmailVerificationLink('user@example.com', 'https://app.example/dashboard?email_verified=1', {
    signInWithOtp: async args => { calls.push(args); return { error: null } },
  })
  assert.deepEqual(calls, [{
    email: 'user@example.com',
    options: {
      shouldCreateUser: false,
      emailRedirectTo: 'https://app.example/dashboard?email_verified=1',
    },
  }])
})

test('a send failure surfaces instead of looking sent', async () => {
  await assert.rejects(
    () => sendEmailVerificationLink('user@example.com', '/dashboard', {
      signInWithOtp: async () => ({ error: new Error('rate limit exceeded') }),
    }),
    /rate limit exceeded/,
  )
  await assert.rejects(
    () => sendEmailVerificationLink('', '/dashboard', { signInWithOtp: async () => ({ error: null }) }),
    /No email address/,
  )
})

test('recording the verification clears the flag and stamps the time', async () => {
  const updated = []
  await markEmailVerified({
    updateUser: async args => { updated.push(args); return { error: null } },
  }, () => '2026-07-29T12:00:00.000Z')

  assert.deepEqual(updated, [{
    data: { needs_email_verification: false, email_verified_at: '2026-07-29T12:00:00.000Z' },
  }])
})

test('a failed metadata write is reported, not swallowed', async () => {
  await assert.rejects(
    () => markEmailVerified({ updateUser: async () => ({ error: new Error('network down') }) }),
    /network down/,
  )
})
