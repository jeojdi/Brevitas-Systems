export const WORKSPACE_TYPE = Object.freeze({
  PERSONAL: 'personal',
  COMPANY: 'company',
})

export const WORKSPACE_NAME_MAX_LENGTH = 100

const VALID_WORKSPACE_TYPES = new Set(Object.values(WORKSPACE_TYPE))

export function normalizeWorkspaceSelection({ workspaceType, workspaceName }) {
  if (!VALID_WORKSPACE_TYPES.has(workspaceType)) {
    throw new Error('Choose a personal or company workspace.')
  }

  const normalizedName = String(workspaceName || '').trim().replace(/\s+/g, ' ')
  if (workspaceType === WORKSPACE_TYPE.COMPANY && !normalizedName) {
    throw new Error('Enter your company name.')
  }
  if (normalizedName.length > WORKSPACE_NAME_MAX_LENGTH) {
    throw new Error(`Workspace names must be ${WORKSPACE_NAME_MAX_LENGTH} characters or fewer.`)
  }

  return {
    workspaceType,
    workspaceName: normalizedName || 'My workspace',
  }
}
