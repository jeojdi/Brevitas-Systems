import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  normalizeWorkspaceSelection,
  WORKSPACE_NAME_MAX_LENGTH,
  WORKSPACE_TYPE,
} from './onboarding-workspace.js'

const component = () => readFile(
  new URL('../components/OnboardingWorkspaceChoice.jsx', import.meta.url),
  'utf8',
)

test('personal onboarding uses a clear one-person default', () => {
  assert.deepEqual(normalizeWorkspaceSelection({
    workspaceType: WORKSPACE_TYPE.PERSONAL,
    workspaceName: '   ',
  }), {
    workspaceType: 'personal',
    workspaceName: 'My workspace',
  })
})

test('company onboarding requires and normalizes the visible company name', () => {
  assert.throws(
    () => normalizeWorkspaceSelection({ workspaceType: WORKSPACE_TYPE.COMPANY, workspaceName: '' }),
    /Enter your company name/,
  )
  assert.deepEqual(normalizeWorkspaceSelection({
    workspaceType: WORKSPACE_TYPE.COMPANY,
    workspaceName: '  Brevitas    Labs  ',
  }), {
    workspaceType: 'company',
    workspaceName: 'Brevitas Labs',
  })
  assert.throws(
    () => normalizeWorkspaceSelection({
      workspaceType: WORKSPACE_TYPE.COMPANY,
      workspaceName: 'x'.repeat(WORKSPACE_NAME_MAX_LENGTH + 1),
    }),
    /100 characters or fewer/,
  )
})

test('workspace choice is keyboard-accessible and explains both onboarding paths', async () => {
  const source = await component()
  assert.match(source, /<fieldset disabled=\{busy\}>/)
  assert.match(source, /type="radio"/)
  assert.match(source, /<legend className="sr-only">Choose a workspace type<\/legend>/)
  assert.match(source, /Personal workspace/)
  assert.match(source, /Company workspace/)
  assert.match(source, /Invite people by email and assign their roles/)
  assert.match(source, /service key for your production backend/)
  assert.match(source, /Joining an existing company\?/)
  assert.match(source, /role="alert"/)
  assert.match(source, /grid grid-cols-1 md:grid-cols-2/)
})
