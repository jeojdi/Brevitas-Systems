import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

test('personal and enterprise entry pages share authentication and preserve server authority', async () => {
  const [auth, app] = await Promise.all([
    readFile(new URL('../components/Auth.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])

  assert.match(auth, /href: '\/login\/personal'/)
  assert.match(auth, /href: '\/login\/enterprise'/)
  assert.match(auth, /signInWithPassword\(\{ email, password \}\)/)
  assert.match(auth, /Your verified membership determines which workspaces you can open/)
  assert.doesNotMatch(auth, /signInWithPassword\([^)]*(?:audience|workspace|company)/)

  assert.match(app, /useState\(\(\) => loginAudienceForPath\(window\.location\.pathname\)\)/)
  assert.match(app, /if \(session && loginAudience\) history\.replaceState\(null, '', '\/dashboard'\)/)
  assert.match(app, /initialWorkspaceType=\{loginAudience === LOGIN_AUDIENCE\.PERSONAL/)
  assert.match(app, /fetchCompanyContext\(session\.access_token/)
  assert.match(app, /companyContext\.companies\.map\(company/)
  assert.match(app, /activateCompany\(session\.access_token, companyId\)/)
})
