import { timingSafeEqual } from 'node:crypto'

/**
 * Parse exactly one RFC 6750 Bearer credential and compare its UTF-8 bytes.
 * @param {string | null} authorization
 * @param {string} expectedToken
 */
export function recoveryBearerAuthorized(authorization, expectedToken) {
  const tokenPattern = /^[A-Za-z0-9._~+/-]+=*$/
  if (!expectedToken || !tokenPattern.test(expectedToken)) return false
  const match = /^Bearer ([A-Za-z0-9._~+/-]+=*)$/i.exec(authorization || '')
  if (!match) return false
  const expected = Buffer.from(expectedToken, 'utf8')
  const supplied = Buffer.from(match[1], 'utf8')
  if (expected.byteLength !== supplied.byteLength) return false
  return timingSafeEqual(expected, supplied)
}
