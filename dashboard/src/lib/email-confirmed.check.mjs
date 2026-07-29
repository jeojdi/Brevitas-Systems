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
    '.check': { textContent: '✓' },
  }
  const document = {
    querySelector(selector) {
      const node = nodes[selector]
      if (!node) throw new Error(`unexpected selector ${selector}`)
      return node
    },
  }
  const location = { search, hash }
  // node:vm rather than `new Function`: the repository SAST policy forbids
  // dynamic code execution (.github/semgrep.yml javascript-dynamic-code-execution),
  // and a sandboxed context is the better tool here regardless.
  runInNewContext(inlineScript, { document, location, URLSearchParams })
  return nodes
}

test('a confirmed session is handed to the SPA instead of being dropped', () => {
  for (const [search, expected] of [
    ['', '/login'],
    ['?audience=personal', '/login/personal'],
    ['?audience=enterprise', '/login/enterprise'],
  ]) {
    const nodes = render({ search, hash: TOKEN_FRAGMENT })
    // The fragment must survive verbatim: auth-js re-parses it on the SPA side and
    // needs access_token, refresh_token, expires_in and token_type all intact.
    assert.equal(nodes['#continue'].href, `${expected}${TOKEN_FRAGMENT}`, search)
    assert.equal(nodes['#continue'].textContent, 'Continue to your dashboard →', search)
    assert.equal(nodes['#title'].textContent, 'Your email is confirmed.', search)
    assert.equal(nodes['.check'].textContent, '✓', search)
  }
})

test('the audience split still routes to its own sign-in page when no session is present', () => {
  const personal = render({ search: '?audience=personal' })
  assert.equal(personal['#continue'].href, '/login/personal')
  assert.equal(personal['#continue'].textContent, 'Continue to personal sign in →')

  const enterprise = render({ search: '?audience=enterprise' })
  assert.equal(enterprise['#continue'].href, '/login/enterprise')
  assert.equal(enterprise['#continue'].textContent, 'Continue to enterprise sign in →')

  const plain = render()
  assert.equal(plain['#continue'].href, '/login')
  assert.equal(plain['#continue'].textContent, 'Continue to sign in →')
})

test('only allowlisted audiences reach the continue link', () => {
  for (const audience of [
    'https://evil.example.com', '//evil.example.com', '../../evil', 'admin', 'PERSONAL',
  ]) {
    const nodes = render({ search: `?audience=${encodeURIComponent(audience)}` })
    assert.equal(nodes['#continue'].href, '/login', audience)
  }
})

test('a failed confirmation link shows the server reason and forwards no fragment', () => {
  // GoTrue reports failures in the fragment; the audience still arrives in the query,
  // which previously masked the fragment entirely.
  const fromHash = render({
    search: '?audience=personal',
    hash: '#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired',
  })
  assert.equal(fromHash['#title'].textContent, 'That link did not work.')
  assert.equal(fromHash['#message'].textContent, 'Email link is invalid or has expired')
  assert.equal(fromHash['.check'].textContent, '!')
  assert.equal(fromHash['#continue'].href, '/login/personal')

  const fromQuery = render({ search: '?error=access_denied&error_description=Token+has+expired' })
  assert.equal(fromQuery['#title'].textContent, 'That link did not work.')
  assert.equal(fromQuery['#message'].textContent, 'Token has expired')
  assert.equal(fromQuery['#continue'].href, '/login')

  const bare = render({ hash: '#error_code=otp_expired' })
  assert.equal(bare['#title'].textContent, 'That link did not work.')
  assert.equal(bare['#message'].textContent,
    'The confirmation link may have expired. Sign in to request another.')
})

test('a half-formed fragment is not treated as a session', () => {
  for (const hash of ['#access_token=abc', '#refresh_token=abc', '#type=signup', '']) {
    const nodes = render({ hash })
    assert.equal(nodes['#continue'].href, '/login', hash)
    assert.equal(nodes['#continue'].textContent, 'Continue to sign in →', hash)
  }
})

test('the confirmation page never hand-rolls auth-js session storage', () => {
  // The point of forwarding the fragment is that auth-js owns the storage record.
  // Writing it here would rot the moment the storage key or flow type changes.
  assert.doesNotMatch(page, /localStorage|sessionStorage|sb-[a-z0-9]+-auth-token/i)
  assert.doesNotMatch(page, /setSession|exchangeCodeForSession|getSessionFromUrl|createClient/i)
  assert.doesNotMatch(page, /supabase[^"']*\.(?:js|co)/i)
  // Guards the deployment_config assertion that this page performs no scripted redirect.
  assert.doesNotMatch(page, /location\.(?:href|assign|replace)\s*=/)
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
