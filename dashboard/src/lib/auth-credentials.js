/**
 * Credential shaping shared by every Supabase auth entry point.
 *
 * Deliberately free of network, React, and validation concerns so the sign-in,
 * sign-up, reset, and recovery paths can agree on exactly one definition of
 * "the same address" and "the same password".
 */

export const PASSWORD_MIN_LENGTH = 6

export const PASSWORD_MISMATCH_MESSAGE = 'Passwords do not match.'

const PASSWORD_TOO_SHORT_MESSAGE = `Password must be at least ${PASSWORD_MIN_LENGTH} characters.`

/**
 * Reduce a typed address to the form the auth server stores.
 *
 * `trim()` is the load-bearing half: a leading or trailing space picked up from
 * autofill or a paste survives all the way to GoTrue, which reports the failed
 * lookup as an indistinguishable "Invalid login credentials".
 *
 * `toLowerCase()` — never `toLocaleLowerCase()` — because the locale-aware form
 * maps ASCII `I` to a dotless `ı` under a Turkish locale and would corrupt an
 * otherwise valid address.
 *
 * Intentionally does not validate. The `type="email"` input and the server stay
 * the only authorities on what a deliverable address is.
 *
 * @param {unknown} value
 * @returns {string}
 */
export function normalizeAuthEmail(value) {
  if (typeof value !== 'string') return ''
  return value.trim().toLowerCase()
}

/**
 * Compare a password against its confirmation.
 *
 * Never trims. A leading or trailing space is a legitimate password character,
 * so accepting a confirmation that matches only after trimming would store a
 * credential the user cannot retype — the lockout this module exists to prevent.
 *
 * @param {unknown} a
 * @param {unknown} b
 * @returns {boolean}
 */
export function passwordsMatch(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false
  return a === b
}

/**
 * Describe why a password is unusable, or return '' when it is acceptable.
 *
 * The floor matches both the `minLength={6}` on the input and the GoTrue
 * default, so the browser, this check, and the server reject the same set. The
 * comparison counts UTF-16 code units, which is also how the HTML `minlength`
 * attribute counts, keeping client-side and in-band validation in agreement.
 *
 * No composition rules are layered on: they would reject passwords that
 * existing accounts already authenticate with.
 *
 * @param {unknown} password
 * @returns {string}
 */
export function passwordStrengthProblem(password) {
  if (typeof password !== 'string' || password.length < PASSWORD_MIN_LENGTH) {
    return PASSWORD_TOO_SHORT_MESSAGE
  }
  return ''
}
