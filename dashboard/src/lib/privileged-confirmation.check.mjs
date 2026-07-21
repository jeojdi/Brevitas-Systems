import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  memberRoleChangeConfirmation,
  memberStatusConfirmation,
  serviceAccountRevocationConfirmation,
} from './privileged-confirmation.js'

const member = { id: 'member_abc123', role: 'member', status: 'active' }

test('member role and status confirmations identify the target and consequence', () => {
  const roleChange = memberRoleChangeConfirmation(member, 'billing_admin')
  assert.match(roleChange.title, /member_abc123/)
  assert.match(roleChange.description, /member to billing admin/)
  assert.match(roleChange.description, /permissions immediately/)

  const disabled = memberStatusConfirmation(member, 'disabled')
  assert.match(disabled.title, /member_abc123/)
  assert.match(disabled.description, /immediately lose company access/)

  const enabled = memberStatusConfirmation({ ...member, status: 'disabled' }, 'active')
  assert.match(enabled.title, /member_abc123/)
  assert.match(enabled.description, /restores company access/)

  const removed = memberStatusConfirmation(member, 'removed')
  assert.match(removed.title, /member_abc123/)
  assert.match(removed.description, /cannot be undone from the dashboard/i)
  assert.throws(() => memberStatusConfirmation(member, 'pending'), /Unsupported member status/)
})

test('service-account revocation confirmation identifies the account and outage risk', () => {
  const confirmation = serviceAccountRevocationConfirmation({ id: 'svc_xyz789', name: 'Production worker' })
  assert.match(confirmation.title, /Production worker/)
  assert.match(confirmation.description, /svc_xyz789/)
  assert.match(confirmation.description, /stop working/)
})

test('privileged actions use an accessible cancellable in-app confirmation dialog', async () => {
  const component = await readFile(new URL('../components/CompanyAdministration.jsx', import.meta.url), 'utf8')
  assert.match(component, /role="alertdialog"/)
  assert.match(component, /aria-modal="true"/)
  assert.match(component, /aria-labelledby="privileged-confirmation-title"/)
  assert.match(component, /aria-describedby="privileged-confirmation-description"/)
  assert.match(component, /event\.key === 'Escape'/)
  assert.match(component, /event\.key !== 'Tab'/)
  assert.match(component, /cancelButtonRef\.current\?\.focus\(\)/)
  assert.match(component, /previousFocus\?\.focus\?\.\(\)/)
  assert.match(component, /error && <p role="alert"/)
  assert.match(component, /requestConfirmation\(memberRoleChangeConfirmation/)
  assert.match(component, /requestConfirmation\(memberStatusConfirmation\(member, 'disabled'\)/)
  assert.match(component, /requestConfirmation\(memberStatusConfirmation\(member, 'active'\)/)
  assert.match(component, /requestConfirmation\(memberStatusConfirmation\(member, 'removed'\)/)
  assert.match(component, /requestConfirmation\(serviceAccountRevocationConfirmation/)
  assert.match(component, />Cancel<\/button>/)
  assert.doesNotMatch(component, /window\.confirm|window\.prompt/)
})
