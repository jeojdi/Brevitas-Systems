import { redactBrowserError } from './api.js'
import { fetchOnboardingStatus } from './onboarding-api.js'

export const ACTIVE_COMPANY_MAX = 100
const COMPANY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const COMPANY_ROLES = new Set(['company_owner', 'company_admin', 'member', 'billing_admin'])

const cleanCompany = value => {
  if (!value || typeof value !== 'object') throw new Error('Invalid company access response')
  const company_id = typeof value.company_id === 'string' ? value.company_id.toLowerCase() : ''
  const company_name = typeof value.company_name === 'string' ? value.company_name.trim() : ''
  const role = typeof value.role === 'string' ? value.role : ''
  const account_type = typeof value.account_type === 'string' ? value.account_type : ''
  if (
    !COMPANY_ID.test(company_id)
    || !company_name || company_name.length > 200 || /[\u0000-\u001f]/.test(company_name)
    || !COMPANY_ROLES.has(role)
    || !['individual', 'company'].includes(account_type)
  ) throw new Error('Invalid company access response')
  return { company_id, company_name, role, account_type }
}

export function normalizeCompanyContext(payload) {
  if (!payload || typeof payload !== 'object' || !Array.isArray(payload.companies)) {
    throw new Error('Invalid company access response')
  }
  const activeCompanyId = typeof payload.company_id === 'string'
    ? payload.company_id.toLowerCase()
    : ''
  if (!COMPANY_ID.test(activeCompanyId) || payload.companies.length > ACTIVE_COMPANY_MAX) {
    throw new Error('Invalid company access response')
  }
  const seen = new Set()
  const companies = payload.companies.map(cleanCompany)
  for (const company of companies) {
    if (seen.has(company.company_id)) throw new Error('Invalid company access response')
    seen.add(company.company_id)
  }
  if (!seen.has(activeCompanyId)) throw new Error('Invalid company access response')
  companies.sort((left, right) => (
    Number(right.company_id === activeCompanyId) - Number(left.company_id === activeCompanyId)
    || left.company_name.localeCompare(right.company_name)
    || left.company_id.localeCompare(right.company_id)
  ))
  return { companies, activeCompanyId }
}

export async function fetchCompanyContext(accessToken, {
  request = fetch,
  signal,
  requestId = () => crypto.randomUUID(),
} = {}) {
  if (typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384) {
    throw new Error('Authentication required')
  }
  let response
  try {
    response = await request('/api/admin/company/capabilities', {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'X-Request-ID': requestId(),
      },
      cache: 'no-store',
      signal,
    })
  } catch (reason) {
    if (reason?.name === 'AbortError') throw reason
    const safeMessage = redactBrowserError(reason instanceof Error ? reason.message : reason)
    throw new Error(safeMessage || 'Company access unavailable')
  }
  if (!response.ok) {
    if (response.status === 403) {
      throw Object.assign(new Error('Company access denied'), { status: 403 })
    }
    if (response.status === 401) {
      throw Object.assign(new Error('Authentication required'), { status: 401 })
    }
    const body = await response.json().catch(() => ({}))
    const safeDetail = redactBrowserError(
      typeof body.detail === 'string' ? body.detail : body.error)
    throw Object.assign(new Error(safeDetail || 'Company access unavailable'), {
      status: response.status,
    })
  }
  const context = normalizeCompanyContext(await response.json().catch(() => null))
  const onboarding = await fetchOnboardingStatus(accessToken, {
    request, requestId, signal,
  })
  if (onboarding.companyId !== context.activeCompanyId) {
    throw new Error('Invalid company access response')
  }
  return { ...context, onboarding }
}

export async function activateCompany(accessToken, companyId, {
  request = fetch,
  requestId = () => crypto.randomUUID(),
} = {}) {
  if (typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384) {
    throw new Error('Authentication required')
  }
  const normalizedCompanyId = typeof companyId === 'string' ? companyId.toLowerCase() : ''
  if (!COMPANY_ID.test(normalizedCompanyId)) throw new Error('Invalid company selection')
  let response
  try {
    response = await request('/api/admin/company/active', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
        'X-Request-ID': requestId(),
      },
      body: JSON.stringify({ company_id: normalizedCompanyId }),
      cache: 'no-store',
    })
  } catch (reason) {
    throw new Error(redactBrowserError(reason instanceof Error ? reason.message : reason)
      || 'Could not switch company')
  }
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw Object.assign(new Error(redactBrowserError(body.detail) || 'Could not switch company'), {
      status: response.status,
    })
  }
  const returnedCompanyId = typeof body.company_id === 'string' ? body.company_id.toLowerCase() : ''
  if (returnedCompanyId !== normalizedCompanyId || !COMPANY_ROLES.has(body.role)) {
    throw new Error('Invalid company access response')
  }
  return { company_id: returnedCompanyId, role: body.role }
}
