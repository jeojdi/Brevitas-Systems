import { createHash, timingSafeEqual } from 'node:crypto'

const tokenPattern = /^[A-Za-z0-9._~+/-]+=*$/
const MINIMUM_SECRET_BYTES = 32
const MAXIMUM_SECRET_BYTES = 256
const DUMMY_SECRET = 'BvtDummy9_qL3mN7xR2pK8wC5sH4jF6zT'

function characterClassCount(value) {
  return [/[a-z]/, /[A-Z]/, /[0-9]/, /[._~+/-]/]
    .filter(pattern => pattern.test(value)).length
}

function isRepeatedPattern(value) {
  for (let period = 1; period <= Math.min(16, value.length / 2); period += 1) {
    if (value.length % period === 0 &&
        value === value.slice(0, period).repeat(value.length / period)) {
      return true
    }
  }
  return false
}

/**
 * Enforce the deployment contract for the dedicated recovery factor. Tokens
 * are ASCII-only so characters and UTF-8 bytes cannot be confused. Operators
 * must still generate the value with a cryptographically secure random source.
 *
 * @param {unknown} candidate
 */
export function recoverySecretIsStrong(candidate) {
  if (typeof candidate !== 'string' || !tokenPattern.test(candidate)) return false
  const byteLength = Buffer.byteLength(candidate, 'utf8')
  if (byteLength !== candidate.length ||
      byteLength < MINIMUM_SECRET_BYTES ||
      byteLength > MAXIMUM_SECRET_BYTES) return false
  if (isRepeatedPattern(candidate)) return false

  // A full 32-byte hexadecimal token is valid. Other supported encodings need
  // multiple character classes and enough diversity to reject placeholders or
  // trivially patterned values while retaining base64/base64url secrets.
  if (/^[A-Fa-f0-9]{64,}$/.test(candidate)) return true
  return characterClassCount(candidate) >= 3 && new Set(candidate).size >= 12
}

function digest(value) {
  return createHash('sha256').update(value, 'utf8').digest()
}

/**
 * Compare a dedicated recovery-secret header without treating it as identity.
 * @param {string | null} suppliedToken
 * @param {string} expectedToken
 */
export function recoverySecretAuthorized(suppliedToken, expectedToken) {
  const expectedValid = recoverySecretIsStrong(expectedToken)
  const suppliedValid = recoverySecretIsStrong(suppliedToken)
  const expectedDigest = digest(expectedValid ? expectedToken : DUMMY_SECRET)
  const suppliedDigest = digest(suppliedValid ? suppliedToken : DUMMY_SECRET)
  const equal = timingSafeEqual(expectedDigest, suppliedDigest)
  return expectedValid && suppliedValid && equal
}
