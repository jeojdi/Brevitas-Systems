import { redactBrowserError } from './api.js'
import { normalizeWorkspaceSelection, WORKSPACE_TYPE } from './onboarding-workspace.js'

const COMPANY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export function normalizeOnboardingStatus(payload) {
  if (!payload || typeof payload !== 'object') throw new Error('Invalid onboarding response')
  const companyId = typeof payload.company_id === 'string' ? payload.company_id.toLowerCase() : ''
  const completedAt = typeof payload.completed_at === 'string' ? payload.completed_at : ''
  if (
    !COMPANY_ID.test(companyId)
    || !['pending', 'complete'].includes(payload.status)
    || typeof payload.cli_connected !== 'boolean'
    || typeof payload.proxied_request_observed !== 'boolean'
    || (payload.status === 'complete') !== Boolean(completedAt)
    || (completedAt && !Number.isFinite(Date.parse(completedAt)))
    || (payload.status === 'complete'
      && (!payload.cli_connected || !payload.proxied_request_observed))
  ) throw new Error('Invalid onboarding response')
  return {
    companyId,
    status: payload.status,
    cliConnected: payload.cli_connected,
    proxiedRequestObserved: payload.proxied_request_observed,
    completedAt,
  }
}

async function requestOnboardingStatus(accessToken, {
  request = fetch,
  requestId = () => crypto.randomUUID(),
  signal,
  complete = false,
} = {}) {
  if (typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384) {
    throw new Error('Authentication required')
  }
  let response
  try {
    response = await request(
      complete
        ? '/v1/organization/onboarding/complete'
        : '/v1/organization/onboarding',
      {
        method: complete ? 'POST' : 'GET',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'X-Request-ID': requestId(),
        },
        cache: 'no-store',
        signal,
      },
    )
  } catch (reason) {
    if (reason?.name === 'AbortError') throw reason
    throw new Error(redactBrowserError(reason instanceof Error ? reason.message : reason)
      || 'Onboarding verification unavailable')
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(redactBrowserError(body.detail) || 'Onboarding verification unavailable')
    error.status = response.status
    throw error
  }
  return normalizeOnboardingStatus(body)
}

export const fetchOnboardingStatus = (accessToken, options = {}) => (
  requestOnboardingStatus(accessToken, { ...options, complete: false })
)

export const completeOnboarding = (accessToken, options = {}) => (
  requestOnboardingStatus(accessToken, { ...options, complete: true })
)

export async function bootstrapWorkspace(accessToken, selection, request = fetch) {
  if (typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384) {
    throw new Error('Authentication required')
  }
  const normalized = normalizeWorkspaceSelection(selection)
  let response
  try {
    response = await request('/v1/organization/bootstrap', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        account_type: normalized.workspaceType === WORKSPACE_TYPE.PERSONAL
          ? 'individual'
          : 'company',
        name: normalized.workspaceName,
      }),
    })
  } catch (reason) {
    throw new Error(redactBrowserError(reason instanceof Error ? reason.message : reason)
      || 'Workspace setup unavailable')
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(redactBrowserError(body.detail) || 'Workspace setup unavailable')
  }
  if (
    typeof body.company_id !== 'string'
    || typeof body.company_name !== 'string'
    || body.role !== 'company_owner'
  ) {
    throw new Error('Invalid workspace setup response')
  }
  return body
}
