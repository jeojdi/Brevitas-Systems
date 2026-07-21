import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(
  new URL('../components/CompanyAdministration.jsx', import.meta.url), 'utf8')

test('service-account creation displays the returned initial key only in memory', () => {
  const creation = component.slice(
    component.indexOf("companyJson('service-accounts'"),
    component.indexOf('Rotate key'),
  )

  assert.match(creation, /const result = await companyJson/)
  assert.match(creation, /setServiceSecret\(result\.api_key\)/)
  assert.match(component, /setServiceSecret\(''\)/)
  assert.match(component, /Brevitas cannot recover it/)
  assert.doesNotMatch(component, /localStorage|sessionStorage|indexedDB/)
})

test('one-time key display supports copy failure and explicit clearing', () => {
  assert.match(component, /navigator\.clipboard\?\.writeText/)
  assert.match(component, /Copy failed/)
  assert.match(component, /Clear from view/)
  assert.match(component, /ph-no-capture/)
  assert.match(component, /data-ph-sensitive/)
})
