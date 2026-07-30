/**
 * Waitlist network bucket: the /24 (IPv4) or /48 (IPv6) dimension the shared
 * limiter never had.
 *
 * WHY. `public.submit_waitlist_signup` (202607200010) enforces exactly two
 * windows: a per-email identity window (3 / 10 min) and ONE global window
 * (120 / min) keyed on an all-zero hash. The global window is the only defence
 * against a source that rotates the email on every request — and because it is
 * global, saturating it locks out every legitimate signup on the site. This
 * module supplies the missing middle dimension, charged BEFORE the global
 * window by src/lib/waitlist-server.ts.
 *
 * WHY A SALTED HASH OF A NETWORK, NOT AN ADDRESS. A raw client IP is personal
 * data and the waitlist limiter table is content-free by design; an UNSALTED
 * hash of an IP is not a pseudonym either, because the entire IPv4 space is
 * 2^32 values and rainbow-tabling it is minutes of work. So the key that leaves
 * this process is HMAC-SHA256(WAITLIST_NETWORK_SALT, "<version>:<network>") and
 * the address itself never leaves the request. Truncating to /24 and /48 first
 * also stops the key from identifying a single subscriber line, and makes the
 * bucket resilient to the address rotation a /64 IPv6 allocation gives away for
 * free.
 *
 * WHY THIS IS A SEPARATE MODULE. The route and src/lib/waitlist-server.ts must
 * keep zero knowledge of forwarding headers — they receive an opaque key — and
 * every branch below has to be executable under `node --test`, which a module
 * importing `server-only` is not.
 *
 * SPOOFING. On Vercel the platform sets `x-vercel-forwarded-for` /
 * `x-real-ip` itself, so those cannot be forged by a client; `x-forwarded-for`
 * is the last-resort fallback for a non-Vercel surface and IS forgeable there.
 * Forging it can only mint a FRESH network bucket, never bypass a limit: the
 * network budget is charged in ADDITION to the global window, which still runs
 * afterwards with its original 120/min ceiling. So an unresolvable or forged
 * address degrades to exactly today's behaviour and nothing more.
 */

import { createHmac } from 'node:crypto'
import { isIP } from 'node:net'

/**
 * Header order matters. The first two are written by Vercel's proxy and
 * overwrite anything the client sent; `x-forwarded-for` is the portable
 * fallback and is only as trustworthy as the surface in front of it.
 */
const CLIENT_ADDRESS_HEADERS = ['x-vercel-forwarded-for', 'x-real-ip', 'x-forwarded-for']

/** Domain separator, so this key can never collide with another HMAC use. */
const NETWORK_KEY_VERSION = 'brevitas.waitlist.network.v1'

/** A salt shorter than this is treated as absent rather than as protection. */
const MINIMUM_SALT_LENGTH = 16

/** Requests per network per window. Deliberately far below the 120/min global. */
export const WAITLIST_NETWORK_LIMIT = 12

/** Window length in seconds; also the ceiling on any returned Retry-After. */
export const WAITLIST_NETWORK_WINDOW_SECONDS = 600

const IPV6_GROUP = /^[0-9a-f]{1,4}$/i

/**
 * Reduce one header value to a single candidate address.
 * `x-forwarded-for` style lists are left-to-right, client first.
 *
 * @param {string} value
 * @returns {string}
 */
function firstHop(value) {
  const [first] = value.split(',')
  return (first || '').trim()
}

/**
 * Strip the decorations a proxy may add: brackets, a zone id, a port.
 *
 * @param {string} candidate
 * @returns {string}
 */
function normalizeAddress(candidate) {
  let address = candidate.trim()
  if (address.startsWith('[')) {
    // "[2001:db8::1]:443"
    const close = address.indexOf(']')
    if (close < 0) return ''
    address = address.slice(1, close)
  }
  const zone = address.indexOf('%')
  if (zone >= 0) address = address.slice(0, zone)
  if (isIP(address)) return address
  // "1.2.3.4:5678" — only ever a port when there is exactly one colon, because
  // an unbracketed IPv6 literal always has at least two.
  const colon = address.indexOf(':')
  if (colon > 0 && colon === address.lastIndexOf(':')) {
    const host = address.slice(0, colon)
    if (isIP(host) === 4) return host
  }
  return isIP(address) ? address : ''
}

/**
 * Expand an IPv6 literal to its eight groups, or null if it is not one this
 * module is prepared to truncate.
 *
 * @param {string} address
 * @returns {string[] | null}
 */
function ipv6Groups(address) {
  const marker = address.indexOf('::')
  let groups
  if (marker < 0) {
    groups = address.split(':')
  } else {
    if (marker !== address.lastIndexOf('::')) return null
    const head = address.slice(0, marker)
    const tail = address.slice(marker + 2)
    const headGroups = head ? head.split(':') : []
    const tailGroups = tail ? tail.split(':') : []
    const missing = 8 - headGroups.length - tailGroups.length
    if (missing < 0) return null
    groups = [...headGroups, ...Array.from({ length: missing }, () => '0'), ...tailGroups]
  }
  // An embedded IPv4 tail ("2001:db8::192.0.2.1") occupies two groups; it can
  // never reach the /48 prefix, so a short expansion is the only failure mode
  // worth rejecting.
  return groups.length >= 3 ? groups : null
}

/**
 * The network this request came from, as a stable canonical string — never a
 * host address. Returns null when no address could be resolved.
 *
 * @param {Headers | { get(name: string): string | null }} headers
 * @returns {string | null}
 */
export function resolveClientNetwork(headers) {
  if (!headers || typeof headers.get !== 'function') return null
  for (const header of CLIENT_ADDRESS_HEADERS) {
    const raw = headers.get(header)
    if (typeof raw !== 'string' || raw === '') continue
    const address = normalizeAddress(firstHop(raw))
    if (!address) continue

    if (isIP(address) === 4) {
      const octets = address.split('.')
      return `v4:${octets[0]}.${octets[1]}.${octets[2]}.0/24`
    }

    // An IPv4-mapped address is an IPv4 client; bucket it with its real peers
    // instead of giving it a private v6 network of its own.
    const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(address)
    if (mapped && isIP(mapped[1]) === 4) {
      const octets = mapped[1].split('.')
      return `v4:${octets[0]}.${octets[1]}.${octets[2]}.0/24`
    }

    const groups = ipv6Groups(address)
    if (!groups) continue
    const prefix = groups.slice(0, 3)
    if (!prefix.every(group => IPV6_GROUP.test(group))) continue
    return `v6:${prefix.map(group => parseInt(group, 16).toString(16)).join(':')}::/48`
  }
  return null
}

/**
 * The opaque `p_network_key` for public.consume_waitlist_network_budget.
 *
 * Null — meaning "charge the global window only" — whenever the salt is absent
 * or too weak to be a salt, or no address could be resolved. Callers MUST treat
 * null as fail-open: a signup form that rejects users because a rate-limit
 * dependency is unconfigured is worse than the abuse it prevents.
 *
 * @param {Headers | { get(name: string): string | null }} headers
 * @param {Record<string, string | undefined>} [environment]
 * @returns {string | null}
 */
export function waitlistNetworkKey(headers, environment = process.env) {
  const salt = (environment?.WAITLIST_NETWORK_SALT || '').trim()
  if (salt.length < MINIMUM_SALT_LENGTH) return null
  const network = resolveClientNetwork(headers)
  if (!network) return null
  return createHmac('sha256', salt)
    .update(`${NETWORK_KEY_VERSION}:${network}`)
    .digest('hex')
}

/**
 * Parse the network budget verdict.
 *
 * A DENIAL fails closed: `allowed: false` always rate-limits, and a missing or
 * out-of-range `retry_after_seconds` becomes the window length rather than
 * discarding the decision. Anything that is not a recognisable verdict throws,
 * and the caller degrades to the global window.
 *
 * @param {unknown} data
 * @returns {{status: 'allowed'} | {status: 'rate_limited', retryAfterSeconds: number}}
 */
export function parseWaitlistNetworkBudget(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid waitlist network budget result')
  }
  if (data.allowed === true) return { status: 'allowed' }
  if (data.allowed === false) {
    const retryAfterSeconds = Number(data.retry_after_seconds)
    const bounded = Number.isSafeInteger(retryAfterSeconds) && retryAfterSeconds >= 1
      ? Math.min(retryAfterSeconds, WAITLIST_NETWORK_WINDOW_SECONDS)
      : WAITLIST_NETWORK_WINDOW_SECONDS
    return { status: 'rate_limited', retryAfterSeconds: bounded }
  }
  throw new Error('Invalid waitlist network budget result')
}
