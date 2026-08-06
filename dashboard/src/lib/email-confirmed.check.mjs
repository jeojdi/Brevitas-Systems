import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const page = read('public/email-confirmed.html')
const inlineScript = page.match(/<script>([\s\S]*?)<\/script>/)?.[1]
assert.ok(inlineScript, 'public/email-confirmed.html must keep exactly one inline script')

const TOKEN_FRAGMENT =
  '#access_token=eyJhbGciOiJIUzI1NiJ9.header.sig&expires_at=1900000000&expires_in=3600'
  + '&refresh_token=r3fr3sh-t0ken&token_type=bearer&type=signup'

/**
 * Execute the page's inline script against a minimal DOM/location stub.
 *
 * The page is static and intentionally SDK-free, so its whole contract is
 * "what does #continue point at, and what does the card say".
 */
function render({ search = '', hash = '' } = {}) {
  const nodes = {
    '#continue': { href: '/login', textContent: 'Continue to sign in →' },
    '#title': { textContent: 'Your email is confirmed.' },
    '#message': { textContent: 'Your account is ready. Sign in to open your token tracking dashboard.' },
    '#spinner': { hidden: true },
    '.check': { textContent: '✓' },
  }
  const document = {
    querySelector(selector) {
      const node = nodes[selector]
      if (!node) throw new Error(`unexpected selector ${selector}`)
      return node
    },
  }
  // Timers are captured, never run: the tests decide when the auto-continue
  // fires so they can check the pre-redirect state (message, spinner, manual
  // link) that a real browser would only show for a moment.
  const timers = []
  const location = {
    search,
    hash,
    replaced: [],
    replace(url) {
      this.replaced.push(url)
    },
  }
  // node:vm rather than `new Function`: the repository SAST policy forbids
  // dynamic code execution (.github/semgrep.yml javascript-dynamic-code-execution),
  // and a sandboxed context is the better tool here regardless.
  runInNewContext(inlineScript, {
    document,
    location,
    URLSearchParams,
    setTimeout(fn, delay) {
      timers.push({ fn, delay })
    },
  })
  return { nodes, location, timers }
}

test('a confirmed session is handed to the SPA instead of being dropped', () => {
  for (const [search, expected] of [
    ['', '/login'],
    ['?audience=personal', '/login/personal'],
    ['?audience=enterprise', '/login/enterprise'],
  ]) {
    const { nodes, location, timers } = render({ search, hash: TOKEN_FRAGMENT })
    // The fragment must survive verbatim: auth-js re-parses it on the SPA side and
    // needs access_token, refresh_token, expires_in and token_type all intact.
    assert.equal(nodes['#continue'].href, `${expected}${TOKEN_FRAGMENT}`, search)
    assert.equal(nodes['#continue'].textContent, 'Continue to your dashboard →', search)
    assert.equal(nodes['#title'].textContent, 'Your email is confirmed.', search)
    assert.equal(nodes['.check'].textContent, '✓', search)

    // The auto-continue must announce itself and keep the manual link intact,
    // because a throttled background tab may never fire the timer.
    assert.match(nodes['#message'].textContent, /taking you to your dashboard/i, search)
    assert.equal(nodes['#spinner'].hidden, false, search)
    assert.equal(timers.length, 1, search)
    assert.ok(
      timers[0].delay >= 500 && timers[0].delay <= 3000,
      `auto-continue delay ${timers[0].delay} should be a beat, not a stall`,
    )
    assert.deepEqual(location.replaced, [], search)
    timers[0].fn()
    // location.replace, with the identical fragment-bearing target as the manual
    // link: replace() keeps the token URL out of history so Back cannot replay it.
    assert.deepEqual(location.replaced, [`${expected}${TOKEN_FRAGMENT}`], search)
  }
})

test('the audience split still routes to its own sign-in page when no session is present', () => {
  const personal = render({ search: '?audience=personal' }).nodes
  assert.equal(personal['#continue'].href, '/login/personal')
  assert.equal(personal['#continue'].textContent, 'Continue to personal sign in →')

  const enterprise = render({ search: '?audience=enterprise' }).nodes
  assert.equal(enterprise['#continue'].href, '/login/enterprise')
  assert.equal(enterprise['#continue'].textContent, 'Continue to enterprise sign in →')

  const plain = render().nodes
  assert.equal(plain['#continue'].href, '/login')
  assert.equal(plain['#continue'].textContent, 'Continue to sign in →')
})

test('only allowlisted audiences reach the continue link', () => {
  for (const audience of [
    'https://evil.example.com', '//evil.example.com', '../../evil', 'admin', 'PERSONAL',
  ]) {
    const nodes = render({ search: `?audience=${encodeURIComponent(audience)}` }).nodes
    assert.equal(nodes['#continue'].href, '/login', audience)
  }
})

test('a failed confirmation link shows the server reason and forwards no fragment', () => {
  // GoTrue reports failures in the fragment; the audience still arrives in the query,
  // which previously masked the fragment entirely.
  const failure = render({
    search: '?audience=personal',
    hash: '#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired',
  })
  const fromHash = failure.nodes
  assert.equal(fromHash['#title'].textContent, 'That link did not work.')
  assert.equal(fromHash['#message'].textContent, 'Email link is invalid or has expired')
  assert.equal(fromHash['.check'].textContent, '!')
  assert.equal(fromHash['#continue'].href, '/login/personal')
  // A dead link must not spin or redirect anywhere; the user has to act.
  assert.equal(fromHash['#spinner'].hidden, true)
  assert.equal(failure.timers.length, 0)

  const fromQuery = render({ search: '?error=access_denied&error_description=Token+has+expired' }).nodes
  assert.equal(fromQuery['#title'].textContent, 'That link did not work.')
  assert.equal(fromQuery['#message'].textContent, 'Token has expired')
  assert.equal(fromQuery['#continue'].href, '/login')

  const bare = render({ hash: '#error_code=otp_expired' }).nodes
  assert.equal(bare['#title'].textContent, 'That link did not work.')
  assert.equal(bare['#message'].textContent,
    'The confirmation link may have expired. Sign in to request another.')
})

test('a half-formed fragment is not treated as a session', () => {
  for (const hash of ['#access_token=abc', '#refresh_token=abc', '#type=signup', '']) {
    const { nodes, timers } = render({ hash })
    assert.equal(nodes['#continue'].href, '/login', hash)
    assert.equal(nodes['#continue'].textContent, 'Continue to sign in →', hash)
    // No session, no auto-continue: redirecting here would strand the user on
    // the sign-in form with a broken fragment instead of an explanation.
    assert.equal(nodes['#spinner'].hidden, true, hash)
    assert.equal(timers.length, 0, hash)
  }
})

test('the confirmation page never hand-rolls auth-js session storage', () => {
  // The point of forwarding the fragment is that auth-js owns the storage record.
  // Writing it here would rot the moment the storage key or flow type changes.
  assert.doesNotMatch(page, /localStorage|sessionStorage|sb-[a-z0-9]+-auth-token/i)
  assert.doesNotMatch(page, /setSession|exchangeCodeForSession|getSessionFromUrl|createClient/i)
  assert.doesNotMatch(page, /supabase[^"']*\.(?:js|co)/i)
  // The auto-continue's `location.replace(...)` call is the one sanctioned
  // redirect: replace() keeps the token-bearing URL out of session history.
  // Assignment forms and `location.assign` stay banned because they would let
  // Back re-surface the fragment, and deployment_config.test.mjs separately
  // forbids the parameter-driven href/assign shapes an open redirect needs.
  assert.doesNotMatch(page, /location\.(?:href|assign)\s*[=(]/)
  assert.doesNotMatch(page, /location\.replace\s*=/)
})

test('every session landing route is still rewritten to the dashboard SPA', () => {
  const config = read('next.config.ts')
  for (const path of [
    '/login', '/login/personal', '/login/enterprise', '/signup', '/waitlist', '/invite', '/dashboard',
  ]) {
    assert.match(
      config,
      new RegExp(`source: '${path.replace(/\//g, '\\/')}', destination: '\\/dashboard\\/index\\.html'`),
      path,
    )
  }
  assert.match(config, /source: '\/email-confirmed', destination: '\/email-confirmed\.html'/)
  // /login* must keep the dashboard CSP, which allows the auth-js call to Supabase.
  assert.match(config, /connect-src 'self' https:\/\/\*\.supabase\.co/)
  for (const path of ['/login', '/login/personal', '/login/enterprise']) {
    assert.match(config, new RegExp(`"${path}"`), `${path} dashboard headers`)
  }
})
