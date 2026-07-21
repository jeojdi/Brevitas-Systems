/**
 * Parse the narrow database admission result. Malformed or legacy responses
 * are dependency failures and must never authorize a recovery attempt.
 *
 * @param {unknown} data
 * @returns {{status: 'accepted'} | {status: 'rate_limited', retryAfterSeconds: number}}
 */
export function parseBillingRecoveryAdmission(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid billing recovery admission result')
  }
  if (data.ok === true && data.code === 'accepted') {
    return { status: 'accepted' }
  }
  if (data.ok === false && data.code === 'rate_limited') {
    const retryAfterSeconds = data.retry_after_seconds
    if (typeof retryAfterSeconds === 'number' &&
        Number.isSafeInteger(retryAfterSeconds) &&
        retryAfterSeconds >= 1 && retryAfterSeconds <= 900) {
      return { status: 'rate_limited', retryAfterSeconds }
    }
  }
  throw new Error('Invalid billing recovery admission result')
}
