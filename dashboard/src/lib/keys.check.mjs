import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { isActiveKeyRow } from './keys.js'

const RAW_KEY = 'bvt_1234567890abcdefghij'
const HASH = createHash('sha256').update(RAW_KEY).digest('hex')
const context = { apiKey: RAW_KEY, apiKeyHash: HASH }

test('hosted RPC rows (prefix only, no fingerprint) identify the active key', () => {
  // company_admin_dashboard_keys_page returns `key_prefix as prefix` = key[:12].
  const self = { id: 'uuid-self', prefix: RAW_KEY.slice(0, 12), key_type: 'dashboard_session' }
  const device = { id: 'uuid-device', prefix: 'bvt_deviceke', key_type: 'device' }
  assert.equal(isActiveKeyRow(self, context), true)
  // A device key must read as revocable — that is the kill switch.
  assert.equal(isActiveKeyRow(device, context), false)
})

test('SQLite rows match on the sha256 fingerprint fallback', () => {
  const self = { id: '1', fingerprint: HASH.slice(0, 16), prefix: '' }
  const other = { id: '2', fingerprint: 'ffffffffffffffff', prefix: '' }
  assert.equal(isActiveKeyRow(self, context), true)
  assert.equal(isActiveKeyRow(other, context), false)
})

test('regression: rows without prefix/fingerprint never match via startsWith("")', () => {
  // The shipped bug: k.fingerprint was always undefined, ''.startsWith('') is
  // true, so EVERY row read as the active key and no Revoke button ever fired.
  for (const row of [{ id: 'x' }, { id: 'x', prefix: '' }, { id: 'x', fingerprint: '' },
                     { id: 'x', prefix: null, fingerprint: null }]) {
    assert.equal(isActiveKeyRow(row, context), false)
  }
  assert.equal(isActiveKeyRow(null, context), false)
  assert.equal(isActiveKeyRow({ id: 'x', prefix: RAW_KEY.slice(0, 12) }, {}), false)
})

test('ApiKeys.jsx uses the shared self-key test, not an invented field', async () => {
  const component = await readFile(new URL('../components/ApiKeys.jsx', import.meta.url), 'utf8')
  assert.match(component, /isActiveKeyRow/)
  assert.match(component, /disabled=\{isActiveKey\(k\)\}/)
  assert.match(component, /isActiveKey\(k\) \? 'Active' : 'Revoke'/)
  assert.doesNotMatch(component, /startsWith\(k\.fingerprint/)
})
