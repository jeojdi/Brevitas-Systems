/**
 * Deferred email verification.
 *
 * Signup no longer blocks on a confirmation link: the account is usable the
 * moment it is created, and proving the address is a task inside the dashboard.
 * That means the "is this address proven?" answer cannot come from GoTrue's
 * `email_confirmed_at`, which is stamped at creation time whenever the project
 * has email confirmations disabled. Instead the signup call marks the new user
 * with `needs_email_verification`, and completing the round trip clears it.
 *
 * Only accounts created by the deferred flow carry the flag, so users who were
 * already confirmed the old way never see the banner.
 *
 * The round trip is a magic link rather than a typed code: Supabase's stock
 * magic-link email contains `{{ .ConfirmationURL }}` and no `{{ .Token }}`, so a
 * code entry box would ask for a number the email never shows.
 *
 * Pure logic: no React, no DOM. Unit-testable in isolation.
 */

/** user_metadata written by signup so the dashboard knows to ask. */
export const PENDING_VERIFICATION_METADATA = Object.freeze({
  needs_email_verification: true,
})

/** Marks the dashboard load that the emailed link redirects back into. */
export const EMAIL_VERIFIED_PARAM = 'email_verified'

export const VERIFICATION_SENT_NOTICE =
  'Check your inbox and click the link to verify this address. You can keep working here.'

export const VERIFICATION_DONE_NOTICE = 'Your email is verified.'

/**
 * Whether the dashboard should still ask this user to prove their address.
 *
 * Absent metadata means "not our concern": an account that predates deferred
 * verification, or one confirmed through the old signup email.
 *
 * @param {{ user_metadata?: Record<string, unknown> }|null|undefined} user
 * @returns {boolean}
 */
export function needsEmailVerification(user) {
  const metadata = user?.user_metadata
  if (typeof metadata !== 'object' || metadata === null) return false
  if (typeof metadata.email_verified_at === 'string' && metadata.email_verified_at) return false
  return metadata.needs_email_verification === true
}

/**
 * Where the emailed link should land: this dashboard, flagged so the app knows
 * to record the verification.
 *
 * The path is fixed rather than taken from the current URL: the link may be
 * opened days later, and it should always land on the dashboard.
 *
 * @param {{ origin: string }} location
 * @returns {string}
 */
export function emailVerificationRedirect(location) {
  return `${String(location?.origin || '')}/dashboard?${EMAIL_VERIFIED_PARAM}=1`
}

/**
 * Whether this page load is the return trip from the verification email.
 *
 * @param {string} search
 * @returns {boolean}
 */
export function isEmailVerificationReturn(search) {
  return new URLSearchParams(String(search || '')).get(EMAIL_VERIFIED_PARAM) === '1'
}

/**
 * The same URL with the verification marker stripped, so a reload or a shared
 * link does not look like a fresh return trip.
 *
 * @param {{ pathname?: string, search?: string, hash?: string }} location
 * @returns {string}
 */
export function urlWithoutVerificationMarker(location) {
  const params = new URLSearchParams(String(location?.search || ''))
  params.delete(EMAIL_VERIFIED_PARAM)
  const query = params.toString()
  return `${location?.pathname || '/'}${query ? `?${query}` : ''}${location?.hash || ''}`
}

/**
 * Email a verification link to an existing account.
 *
 * `shouldCreateUser: false` matters: without it a stale address would silently
 * create a second account instead of failing.
 *
 * @param {string} email
 * @param {string} redirectTo
 * @param {{ signInWithOtp: Function }} auth
 * @returns {Promise<void>}
 */
export async function sendEmailVerificationLink(email, redirectTo, auth) {
  if (typeof email !== 'string' || !email) throw new Error('No email address on this account.')
  const { error } = await auth.signInWithOtp({
    email,
    options: { shouldCreateUser: false, emailRedirectTo: redirectTo },
  })
  if (error) throw error
}

/**
 * Record that the address was proven, clearing the pending flag.
 *
 * Called only after the emailed link produced a session for this user, which is
 * what makes the claim true.
 *
 * @param {{ updateUser: Function }} auth
 * @param {() => string} [now]
 * @returns {Promise<void>}
 */
export async function markEmailVerified(auth, now = () => new Date().toISOString()) {
  const { error } = await auth.updateUser({
    data: { needs_email_verification: false, email_verified_at: now() },
  })
  if (error) throw error
}
