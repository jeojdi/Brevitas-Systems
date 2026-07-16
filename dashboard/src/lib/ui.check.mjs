import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

test('savings UI makes no fee, payment, or amount-owed claim', async () => {
  const billing = await source('Billing')
  assert.doesNotMatch(billing, /\b(fee|charged?|payment|owed?)\b/i)
})

test('dashboard navigation is separated and exposes its active section', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /aria-label="Dashboard sections"/)
  assert.match(app, /aria-current=\{activeTab === tab \? 'page' : undefined\}/)
})
