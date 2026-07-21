/**
 * Parse the narrow, server-only database admission result without copying
 * database errors or lead data into the route response.
 *
 * @param {unknown} data
 * @returns {{status: 'accepted'} | {status: 'rate_limited', retryAfterSeconds: number}}
 */
export function parseWaitlistAdmission(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid shared waitlist admission result')
  }
  if (data.ok === true && data.code === 'accepted') {
    return { status: 'accepted' }
  }
  if (data.ok === false && data.code === 'rate_limited') {
    const retryAfterSeconds = Number(data.retry_after_seconds)
    if (Number.isSafeInteger(retryAfterSeconds) &&
        retryAfterSeconds >= 1 && retryAfterSeconds <= 600) {
      return { status: 'rate_limited', retryAfterSeconds }
    }
  }
  throw new Error('Invalid shared waitlist admission result')
}
