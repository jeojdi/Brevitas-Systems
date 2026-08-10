/**
 * Copy and session state for repeated signup submissions.
 *
 * GoTrue answers a signup for an address that already exists by resending the
 * confirmation email and discarding the rest of the request, so the password in
 * that second submission is never stored. A user who reads the post-signup copy
 * as a rejection signs up again, confirms, and can then never log in with the
 * password they just chose. The copy below states what actually happened, and
 * the tracker lets the form recognize a repeat submission before it pretends a
 * new password was saved.
 *
 * Pure logic: no React, no Supabase client, no DOM. Unit-testable in isolation.
 */

import { normalizeAuthEmail, PASSWORD_MISMATCH_MESSAGE } from './auth-credentials.js'

export const SIGNUP_CONFIRMATION_NOTICE =
  'We sent a confirmation link to your email, and you need to click it to activate your account. '
  + 'Delivery can take a minute or two, so check your spam folder if it has not arrived. '
  + 'If you already have an account with this address, use "Forgot password" instead of signing up '
  + 'again, because a second signup will not change your password.'

export const DUPLICATE_SIGNUP_NOTICE =
  'You already submitted this email, and signing up again does not save a new password. '
  + 'Click the confirmation link in your inbox, resend that email if you cannot find it, or use '
  + '"Forgot password" to set a password you can sign in with.'

export const SIGNUP_TRACKER_MAX_ENTRIES = 64

/**
 * Bucket a failed signup into a coarse, non-PII reason for analytics.
 *
 * `signup_started` fires on the button press and `signup_submitted` only after
 * GoTrue accepts the account, so before this existed every failure was
 * denominator-only: the funnel could show that an attempt died but never why,
 * and an abandoned form was indistinguishable from a server error. These buckets
 * close that gap while carrying no address, password, or raw provider text —
 * only the caller's already-sanitized message is inspected, and nothing derived
 * from it is returned.
 *
 * `emailDeliveryFailed` is passed in rather than re-derived, because the pattern
 * that recognizes GoTrue's "Error sending ... email" lives in supabase.js next to
 * the copy it maps to, and this module is deliberately free of that import.
 *
 * @param {unknown} message  a user-facing message from `authErrorMessage`
 * @param {{ emailDeliveryFailed?: boolean }} [options]
 * @returns {string}
 */
export function signupFailureReason(message, { emailDeliveryFailed = false } = {}) {
  if (emailDeliveryFailed) return 'email_delivery'
  const raw = String(message ?? '').trim()
  if (!raw) return 'unknown'
  if (raw === PASSWORD_MISMATCH_MESSAGE) return 'password_mismatch'
  const text = raw.toLowerCase()
  if (text.includes('rate limit')) return 'rate_limited'
  if (text.includes('already registered') || text.includes('already exists')) return 'duplicate_email'
  if (text.includes('password')) return 'weak_password'
  // Browsers word a dead connection three different ways; none of them reach GoTrue.
  if (text.includes('failed to fetch') || text.includes('networkerror') || text.includes('load failed')) {
    return 'network'
  }
  return 'auth_error'
}

/**
 * Normalize an email for comparison.
 *
 * Deliberately the same function the form sends to Supabase, so "already
 * submitted" means the same thing here as it does to GoTrue. An earlier version
 * also stripped internal whitespace, which let the tracker call two addresses
 * identical that Supabase treats as distinct.
 *
 * @param {unknown} email
 * @returns {string}
 */
const normalizeEmail = normalizeAuthEmail

/**
 * Track which addresses this browser session already submitted to signup.
 *
 * The store is bounded and evicts the least recently recorded address, so a
 * long-lived tab cannot grow it without limit.
 *
 * @param {number} [maxEntries]
 * @returns {{
 *   hasAttempted: (email: unknown) => boolean,
 *   record: (email: unknown) => void,
 *   reset: () => void,
 * }}
 */
export function createSignupTracker(maxEntries = SIGNUP_TRACKER_MAX_ENTRIES) {
  const attempted = new Set()

  return {
    hasAttempted(email) {
      const normalized = normalizeEmail(email)
      return Boolean(normalized) && attempted.has(normalized)
    },
    record(email) {
      const normalized = normalizeEmail(email)
      if (!normalized) return
      // Re-record refreshes recency, matching the session key cache in supabase.js.
      attempted.delete(normalized)
      attempted.add(normalized)
      while (attempted.size > maxEntries) {
        attempted.delete(attempted.values().next().value)
      }
    },
    reset() {
      attempted.clear()
    },
  }
}
