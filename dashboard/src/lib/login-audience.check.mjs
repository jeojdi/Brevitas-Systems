import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

/**
 * The balanced argument text of the first `name(...)` call in `source`.
 *
 * Returns null when the call is absent. A preceding `.` is allowed so both
 * `supabase.auth.signUp(` and a bare `signUp(` are found, while a longer
 * identifier ending in the same characters is not.
 *
 * This exists so assertions can talk about *what a call receives* rather than
 * how its arguments happen to be spelled. Matching a literal argument list
 * couples the test to formatting: the moment a value is wrapped in a helper,
 * renamed, or broken across lines, the assertion fails without the guarantee
 * having changed at all.
 */
function callArguments(source, name) {
  const opening = new RegExp(`(?<![\\w$])${name}\\s*\\(`).exec(source)
  if (!opening) return null
  const start = opening.index + opening[0].length
  let depth = 1
  for (let index = start; index < source.length; index += 1) {
    if (source[index] === '(') depth += 1
    else if (source[index] === ')') {
      depth -= 1
      if (depth === 0) return source.slice(start, index)
    }
  }
  return null
}

test('personal and enterprise entry pages share authentication and preserve server authority', async () => {
  const [auth, app] = await Promise.all([
    readFile(new URL('../components/Auth.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])

  assert.match(auth, /href: '\/login\/personal'/)
  assert.match(auth, /href: '\/login\/enterprise'/)
  assert.match(auth, /Your verified membership determines which workspaces you can open/)

  // The guarantee here is semantic, not textual: whichever audience page the
  // user came through, signing in is one Supabase password grant that carries
  // an email and a password and nothing else. The audience is presentation
  // only — the server decides which workspaces the account may open — so no
  // audience/workspace/company selector may ride along in the grant.
  //
  // Previously this pinned the literal `signInWithPassword({ email, password })`.
  // That asserted the argument list's spelling rather than its content, so a
  // change as innocuous as normalizing the address before submitting it broke
  // the test while leaving the guarantee fully intact. Inspecting the balanced
  // argument text instead accepts any spelling of the same call and is in fact
  // stricter about the thing that matters: the old `[^)]*` negative lookahead
  // stopped scanning at the first `)`, so a nested call would have hidden a
  // smuggled selector from it.
  const passwordGrant = callArguments(auth, 'signInWithPassword')
  assert.ok(passwordGrant, 'login must authenticate through the Supabase password grant')
  assert.match(passwordGrant, /\bemail\b/)
  assert.match(passwordGrant, /\bpassword\b/)
  assert.doesNotMatch(passwordGrant, /audience|workspace|company/i)

  // Login and account creation stay distinct calls: signup must remain a real
  // signUp with a confirmation redirect, never a login wearing different copy.
  const accountCreation = callArguments(auth, 'signUp')
  assert.ok(accountCreation, 'signup must create an account through Supabase signUp')
  assert.match(accountCreation, /\bemail\b/)
  assert.match(accountCreation, /emailRedirectTo/)

  assert.match(app, /useState\(\(\) => loginAudienceForPath\(window\.location\.pathname\)\)/)
  assert.match(app, /if \(session && loginAudience\) history\.replaceState\(null, '', '\/dashboard'\)/)
  assert.match(app, /activeWorkspace\?\.account_type === 'individual'/)
  assert.match(app, /loginAudience === LOGIN_AUDIENCE\.PERSONAL/)
  assert.match(app, /fetchCompanyContext\(session\.access_token/)
  assert.match(app, /const PERSONAL_TABS/)
  assert.match(app, /const ENTERPRISE_TABS/)
  assert.match(app, /enterpriseWorkspace \? ENTERPRISE_TABS : PERSONAL_TABS/)
  assert.match(app, /companyContext\.companies\.map\(company/)
  assert.match(app, /activateCompany\(session\.access_token, companyId\)/)
})
