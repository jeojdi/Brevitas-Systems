import { redactBrowserError } from './api.js'
import { normalizeWorkspaceSelection, WORKSPACE_TYPE } from './onboarding-workspace.js'

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
