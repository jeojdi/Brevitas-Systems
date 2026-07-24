const INVITATION_TOKEN = /^bvi_[A-Za-z0-9_-]+$/
const REQUEST_ID = /^[A-Za-z0-9._:-]{8,128}$/
const COMPANY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const ACCEPTED_ROLES = new Set(['company_admin', 'member', 'billing_admin'])
const FRAGMENT_KEYS = new Set(['invite', 'invitation', 'invitation_token'])

export const COMPANY_INVITATION_ENDPOINT = '/api/admin/company/invitations/accept'

export function isCompanyInvitationToken(value) {
  return typeof value === 'string'
    && value.length >= 40
    && value.length <= 128
    && INVITATION_TOKEN.test(value)
}

function scrubFragment(locationObject, historyObject, retainedFragment = '') {
  const path = `${locationObject.pathname || ''}${locationObject.search || ''}`
  const cleanUrl = `${path}${retainedFragment ? `#${retainedFragment}` : ''}`
  try {
    if (typeof historyObject?.replaceState !== 'function') throw new TypeError('History unavailable')
    historyObject.replaceState(historyObject.state ?? null, '', cleanUrl)
  } catch {
    // replaceState can be unavailable in constrained webviews. Hash assignment is
    // a best-effort fallback and still removes the secret from the visible URL.
    try { locationObject.hash = retainedFragment ? `#${retainedFragment}` : '' } catch { /* noop */ }
  }
}

/**
 * Reads an invitation from a URL fragment and removes only the invitation part.
 * Canonical links use `#invite=bvi_...`; `#bvi_...`, `invitation`, and
 * `invitation_token` are accepted for backwards-compatible email links.
 *
 * The returned secret is never persisted. Callers should keep it in memory only.
 */
export function consumeCompanyInvitationFragment({
  location: locationObject = globalThis.location,
  history: historyObject = globalThis.history,
} = {}) {
  const hash = typeof locationObject?.hash === 'string'
    ? locationObject.hash.replace(/^#/, '')
    : ''
  if (!hash) return { found: false, token: '' }

  let directValue = ''
  try { directValue = decodeURIComponent(hash) } catch { directValue = hash }
  if (directValue.startsWith('bvi_')) {
    scrubFragment(locationObject, historyObject)
    return {
      found: true,
      token: isCompanyInvitationToken(directValue) ? directValue : '',
    }
  }

  const params = new URLSearchParams(hash)
  const candidates = []
  let found = false
  for (const [key, value] of params.entries()) {
    if (!FRAGMENT_KEYS.has(key.toLowerCase())) continue
    found = true
    candidates.push(value)
  }
  if (!found) return { found: false, token: '' }

  for (const key of [...params.keys()]) {
    if (FRAGMENT_KEYS.has(key.toLowerCase())) params.delete(key)
  }
  scrubFragment(locationObject, historyObject, params.toString())

  // Ambiguous links are scrubbed but never acted on.
  const token = candidates.length === 1 && isCompanyInvitationToken(candidates[0])
    ? candidates[0]
    : ''
  return { found: true, token }
}

export class CompanyInvitationError extends Error {
  constructor(message, { code = 'invitation_unavailable', status = 0, retryable = false } = {}) {
    super(message)
    this.name = 'CompanyInvitationError'
    this.code = code
    this.status = status
    this.retryable = retryable
  }
}

function invitationError(status) {
  if (status === 401) {
    return new CompanyInvitationError(
      'Your session expired. Sign in again to accept this invitation.',
      { code: 'authentication_required', status, retryable: true },
    )
  }
  if (status === 403) {
    return new CompanyInvitationError(
      'This invitation belongs to a different verified email address or is no longer available.',
      { code: 'invitation_forbidden', status },
    )
  }
  if (status === 404) {
    return new CompanyInvitationError(
      'This invitation is invalid, expired, cancelled, or already used.',
      { code: 'invitation_not_found', status },
    )
  }
  if (status === 409) {
    return new CompanyInvitationError(
      'This account already has a membership that conflicts with the invitation.',
      { code: 'membership_conflict', status },
    )
  }
  if (status === 400 || status === 413 || status === 422) {
    return new CompanyInvitationError(
      'This invitation link is not valid.',
      { code: 'invalid_invitation', status },
    )
  }
  if (status === 429) {
    return new CompanyInvitationError(
      'Too many attempts. Wait a moment, then try again.',
      { code: 'rate_limited', status, retryable: true },
    )
  }
  return new CompanyInvitationError(
    'Invitation acceptance is temporarily unavailable. Try again.',
    { code: 'invitation_unavailable', status, retryable: status >= 500 },
  )
}

function normalizeAcceptedInvitation(payload) {
  const companyId = typeof payload?.company_id === 'string' ? payload.company_id.toLowerCase() : ''
  const role = typeof payload?.role === 'string' ? payload.role : ''
  if (
    !COMPANY_ID.test(companyId)
    || !ACCEPTED_ROLES.has(role)
    || payload?.status !== 'accepted'
  ) {
    throw new CompanyInvitationError(
      'Invitation acceptance returned an invalid response.',
      { code: 'invalid_response', retryable: true },
    )
  }
  return { company_id: companyId, role, status: 'accepted' }
}

export async function acceptCompanyInvitation(accessToken, invitationToken, {
  request = fetch,
  requestId = () => crypto.randomUUID(),
  signal,
} = {}) {
  if (typeof accessToken !== 'string' || accessToken.length < 20 || accessToken.length > 8192) {
    throw new CompanyInvitationError(
      'Sign in before accepting this invitation.',
      { code: 'authentication_required', status: 401, retryable: true },
    )
  }
  if (!isCompanyInvitationToken(invitationToken)) {
    throw new CompanyInvitationError(
      'This invitation link is not valid.',
      { code: 'invalid_invitation', status: 400 },
    )
  }
  const traceId = requestId()
  if (typeof traceId !== 'string' || !REQUEST_ID.test(traceId)) {
    throw new CompanyInvitationError(
      'Invitation acceptance could not be started. Try again.',
      { code: 'request_unavailable', retryable: true },
    )
  }

  let response
  try {
    response = await request(COMPANY_INVITATION_ENDPOINT, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
        'X-Request-ID': traceId,
      },
      body: JSON.stringify({ invitation_token: invitationToken }),
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
      referrerPolicy: 'no-referrer',
      signal,
    })
  } catch (reason) {
    if (reason?.name === 'AbortError') throw reason
    throw new CompanyInvitationError(
      'Invitation acceptance is temporarily unavailable. Try again.',
      { code: 'network_unavailable', retryable: true },
    )
  }

  if (!response.ok) {
    try { await response.body?.cancel() } catch { /* discard untrusted error details */ }
    throw invitationError(response.status)
  }
  const payload = await response.json().catch(() => null)
  return normalizeAcceptedInvitation(payload)
}
