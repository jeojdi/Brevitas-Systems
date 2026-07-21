/**
 * Parse the narrow server-only database result. Legacy or malformed values are
 * dependency failures and cannot authorize Checkout or Customer Portal work.
 *
 * @param {unknown} data
 * @returns {{status: 'accepted'} | {status: 'rate_limited', retryAfterSeconds: number}}
 */
export function parseBillingControlAdmission(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid billing control admission result')
  }
  if (data.ok === true && data.code === 'accepted') {
    return { status: 'accepted' }
  }
  if (data.ok === false && data.code === 'rate_limited') {
    const retryAfterSeconds = data.retry_after_seconds
    if (typeof retryAfterSeconds === 'number' &&
        Number.isSafeInteger(retryAfterSeconds) &&
        retryAfterSeconds >= 1 && retryAfterSeconds <= 300) {
      return { status: 'rate_limited', retryAfterSeconds }
    }
  }
  throw new Error('Invalid billing control admission result')
}
