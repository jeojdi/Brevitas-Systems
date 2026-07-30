// I11 (docs/STRIPE_TEST_PLAN.md §2.2) — the /api/billing/sync input ladder AND
// its order, executed against the real handler.
//
// This route is the only way an operator can move a billing_ledger row by hand
// (`resolution: 'reported' | 'dead' | 'pending'`), so both WHAT it rejects and
// the ORDER in which it rejects are security properties:
//
//   * the recovery secret is a SECOND FACTOR, never an identity. It must not be
//     read — not even compared — until Supabase has authenticated the actor,
//     canonical company authorization has succeeded, and the shared attempt has
//     been admitted (src/app/api/billing/sync/route.ts:63-84). Otherwise the
//     endpoint becomes an unauthenticated secret-guessing oracle with no rate
//     limit in front of it.
//   * a WEAK configured secret must 503 (misconfiguration), not 401 (rejected
//     caller), because the two are diagnosed completely differently.
//
// The ordering half is proved by handing the route a duck-typed request whose
// `headers.get` records every name it is asked for, so "was the header read?" is
// a measurement rather than an inference.
//
// The fixture secret is GENUINELY STRONG and the file asserts that up front: a
// weak fixture makes `recoverySecretIsStrong(config.recoverySecret)` false, and
// then every "401 mismatch" assertion below would silently be asserting the 503
// misconfiguration branch instead (src/lib/billing/recovery-auth.mjs:30-43).
//
// Run: node --test tests/billing_sync_input_ladder.test.mjs

import assert from 'node:assert/strict'
import test from 'node:test'

import { recoverySecretIsStrong } from '../src/lib/billing/recovery-auth.mjs'
import {
  loadBillingRoute,
  loaderUnavailable,
  readRepositoryFile,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const STRONG_SECRET = 'Bv7q_R3nX9pL2sK8wM4tC6zH5jF1yD0aQ'
const WEAK_SECRET = 'a'.repeat(64)
const USER_ID = '33333333-3333-4333-8333-333333333333'
const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const VALID_BODY = { entry_id: 41, resolution: 'reported', note: 'operator resolved by hand' }

test('the fixture secret is strong and the weak one is genuinely weak', () => {
  // Guard the guard. If this flips, every 401-vs-503 assertion below inverts.
  assert.equal(recoverySecretIsStrong(STRONG_SECRET), true)
  assert.equal(recoverySecretIsStrong(WEAK_SECRET), false)
})

test('the route states its second-factor ordering in source, in order', () => {
  const route = readRepositoryFile('src/app/api/billing/sync/route.ts')
  const at = needle => {
    const index = route.indexOf(needle)
    assert.notEqual(index, -1, needle)
    return index
  }
  // Gate -> authenticate -> authorize -> admit -> only then look at the header.
  assert.ok(at('billingMaintenanceResponse()') < at('authenticatedBillingUser('))
  assert.ok(at('authenticatedBillingUser(') < at('authorizeActiveBillingCompany('))
  assert.ok(at('authorizeActiveBillingCompany(') < at('consumeBillingRecoveryAttempt('))
  assert.ok(at('consumeBillingRecoveryAttempt(') < at("'x-billing-recovery-secret'"))
  // The strength check must precede the comparison, so a misconfigured
  // deployment cannot be probed as if it were rejecting a caller.
  assert.ok(at('recoverySecretIsStrong(recoverySecret)') < at('recoverySecretAuthorized('))
})

// ---------------------------------------------------------------------------
// Stubs (registered before the first route import; see the loader's contract)
// ---------------------------------------------------------------------------

class BillingRecoveryAdmissionError extends Error {
  constructor(message = 'billing recovery admission failed') {
    super(message)
    this.name = 'BillingRecoveryAdmissionError'
  }
}

let scenario = null
const current = () => {
  if (!scenario) throw new Error('no sync scenario is installed')
  return scenario
}

if (loaderUnavailable === false) {
  stubModule('@/lib/billing/config', {
    billingConfig: () => ({ recoverySecret: current().configuredSecret }),
  })
  stubModule('@/lib/billing/supabase', {
    BillingRecoveryAdmissionError,
    authenticatedBillingUser: async request => {
      current().calls.push('authenticatedBillingUser')
      // The real implementation reads the Authorization header; recording that
      // read here would make the header-order assertion meaningless, so the
      // stub deliberately does not touch `request`.
      void request
      return current().user
    },
    authorizeActiveBillingCompany: async userId => {
      current().calls.push(`authorizeActiveBillingCompany:${userId}`)
      return current().authorization
    },
    consumeBillingRecoveryAttempt: async (userId, organizationId) => {
      current().calls.push(`consumeBillingRecoveryAttempt:${userId}:${organizationId}`)
      if (current().admission instanceof Error) throw current().admission
      return current().admission
    },
    manuallyResolveBillingLedgerEntry: async values => {
      current().calls.push('manuallyResolveBillingLedgerEntry')
      current().resolutions.push(values)
      return current().resolution
    },
  })
}

function installScenario(overrides = {}) {
  scenario = {
    configuredSecret: STRONG_SECRET,
    user: { id: USER_ID },
    authorization: { ok: true, organizationId: ORGANIZATION_ID },
    admission: { status: 'accepted' },
    resolution: { ok: true, code: 'resolved', auditId: 907 },
    calls: [],
    resolutions: [],
    ...overrides,
  }
  return scenario
}

/**
 * A duck-typed Request. The handler only ever calls `headers.get(name)` and
 * `text()`, so this is a faithful stand-in AND it records exactly which headers
 * were read, which is the only way to prove the second factor was not consulted
 * early.
 */
function recordingRequest({ headers = {}, body = JSON.stringify(VALID_BODY) } = {}) {
  const normalized = new Map(
    Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value]),
  )
  const reads = []
  let textReads = 0
  return {
    reads,
    textReads: () => textReads,
    request: {
      method: 'POST',
      headers: {
        get(name) {
          reads.push(String(name).toLowerCase())
          const value = normalized.get(String(name).toLowerCase())
          return value === undefined ? null : value
        },
      },
      async text() {
        textReads += 1
        return body
      },
    },
  }
}

function authorizedHeaders(extra = {}) {
  return {
    'content-type': 'application/json',
    'x-billing-recovery-secret': STRONG_SECRET,
    ...extra,
  }
}

const skip = loaderUnavailable

async function post(options) {
  const { POST } = await loadBillingRoute('sync')
  const recorder = recordingRequest(options)
  const response = await POST(recorder.request)
  return { response, recorder, body: await response.json() }
}

// The maintenance gate reads process.env directly (maintenance-gate.mjs:4), so
// the launch gate is genuine environment state in these tests, not a stub.
const originalGate = process.env.BREVITAS_BILLING_ENABLED

function withGate(value, run) {
  const previous = process.env.BREVITAS_BILLING_ENABLED
  if (value === undefined) delete process.env.BREVITAS_BILLING_ENABLED
  else process.env.BREVITAS_BILLING_ENABLED = value
  return (async () => {
    try {
      return await run()
    } finally {
      if (previous === undefined) delete process.env.BREVITAS_BILLING_ENABLED
      else process.env.BREVITAS_BILLING_ENABLED = previous
    }
  })()
}

const enabled = run => withGate('true', run)

// ---------------------------------------------------------------------------
// Ordering: nothing before the second factor may read it
// ---------------------------------------------------------------------------

test('the launch gate answers before any header, body, or Supabase call', { skip }, async () => {
  installScenario()
  for (const value of [undefined, 'false', 'TRUE', ' true ']) {
    const result = await withGate(value, () => post({ headers: authorizedHeaders() }))
    assert.equal(result.response.status, 503, JSON.stringify(value))
    assert.equal(result.response.headers.get('retry-after'), '30')
    assert.equal(result.response.headers.get('cache-control'), 'no-store')
    assert.deepEqual(result.body, { error: 'Billing is temporarily unavailable' })
    assert.deepEqual(result.recorder.reads, [], 'the gate must not read a header')
    assert.equal(result.recorder.textReads(), 0)
    assert.deepEqual(scenario.calls, [], 'the gate must precede every Supabase call')
  }
})

test('an unauthenticated caller is refused without the recovery header being read', { skip }, async () => {
  installScenario({ user: null })
  const { response, recorder, body } = await enabled(() => post({
    // A caller who guessed a WRONG secret still gets the auth answer, so the
    // endpoint reveals nothing about the secret to an unauthenticated prober.
    headers: authorizedHeaders({ 'x-billing-recovery-secret': 'wrong-but-well-formed-secret' }),
  }))
  assert.equal(response.status, 401)
  assert.deepEqual(body, { error: 'Authentication required' })
  assert.ok(!recorder.reads.includes('x-billing-recovery-secret'))
  assert.equal(recorder.textReads(), 0)
  assert.deepEqual(scenario.calls, ['authenticatedBillingUser'])
})

test('an unauthorized company is refused without the recovery header being read', { skip }, async () => {
  installScenario({ authorization: { ok: false, code: 'forbidden', organizationId: null } })
  const { response, recorder, body } = await enabled(() => post({
    headers: authorizedHeaders(),
  }))
  assert.equal(response.status, 403)
  assert.deepEqual(body, { error: 'Billing permission is required for the active company' })
  assert.ok(!recorder.reads.includes('x-billing-recovery-secret'))
  assert.deepEqual(scenario.calls, [
    'authenticatedBillingUser',
    `authorizeActiveBillingCompany:${USER_ID}`,
  ])

  // An `ok: true` authorization with no organization id must also fail closed:
  // the organization id is what scopes the ledger mutation.
  installScenario({ authorization: { ok: true, organizationId: null } })
  const second = await enabled(() => post({ headers: authorizedHeaders() }))
  assert.equal(second.response.status, 403)
  assert.ok(!second.recorder.reads.includes('x-billing-recovery-secret'))
})

test('admission failures and rate limits precede the second factor', { skip }, async () => {
  // A broken admission RPC is a 503 with a short retry, distinct from the 300 s
  // misconfiguration retry below.
  installScenario({ admission: new BillingRecoveryAdmissionError() })
  const unavailable = await enabled(() => post({ headers: authorizedHeaders() }))
  assert.equal(unavailable.response.status, 503)
  assert.equal(unavailable.response.headers.get('retry-after'), '5')
  assert.deepEqual(unavailable.body, { error: 'Billing recovery is temporarily unavailable' })
  assert.ok(!unavailable.recorder.reads.includes('x-billing-recovery-secret'))
  assert.deepEqual(scenario.calls, [
    'authenticatedBillingUser',
    `authorizeActiveBillingCompany:${USER_ID}`,
    `consumeBillingRecoveryAttempt:${USER_ID}:${ORGANIZATION_ID}`,
  ])

  // An admission error that is NOT the admission type must not be swallowed as
  // a 503; it propagates so the platform 500s and the failure is visible.
  installScenario({ admission: new Error('unexpected') })
  await assert.rejects(
    enabled(() => post({ headers: authorizedHeaders() })),
    /unexpected/,
  )

  installScenario({ admission: { status: 'rate_limited', retryAfterSeconds: 42 } })
  const limited = await enabled(() => post({ headers: authorizedHeaders() }))
  assert.equal(limited.response.status, 429)
  assert.equal(limited.response.headers.get('retry-after'), '42')
  assert.equal(limited.response.headers.get('cache-control'), 'no-store')
  assert.deepEqual(limited.body, { error: 'Too many billing recovery attempts' })
  // The rate limiter, not the secret, is what stops a flood of guesses.
  assert.ok(!limited.recorder.reads.includes('x-billing-recovery-secret'))
  assert.equal(limited.recorder.textReads(), 0)
})

// ---------------------------------------------------------------------------
// The second factor: 503 misconfiguration vs 401 rejection
// ---------------------------------------------------------------------------

test('a weak configured secret is a 503 with a long retry, never a 401', { skip }, async () => {
  for (const configuredSecret of ['', WEAK_SECRET, 'short', `${STRONG_SECRET} `]) {
    installScenario({ configuredSecret })
    const { response, recorder, body } = await enabled(() => post({
      // Supplying the *correct* value cannot help: the deployment itself is
      // misconfigured, and answering 401 here would send an operator hunting
      // for a bad client instead of a bad environment variable.
      headers: authorizedHeaders({ 'x-billing-recovery-secret': configuredSecret }),
    }))
    assert.equal(response.status, 503, JSON.stringify(configuredSecret))
    assert.equal(response.headers.get('retry-after'), '300')
    assert.equal(response.headers.get('cache-control'), 'no-store')
    assert.deepEqual(body, { error: 'Billing recovery is temporarily unavailable' })
    // The admission was still consumed first: a misconfigured deployment is not
    // a free-attempt bypass.
    assert.ok(scenario.calls.some(call => call.startsWith('consumeBillingRecoveryAttempt')))
    assert.equal(recorder.textReads(), 0)
  }
})

test('a missing or mismatched second factor is a 401 that reads no body', { skip }, async () => {
  const rejected = [
    undefined,
    '',
    'x',
    WEAK_SECRET,
    `${STRONG_SECRET}x`,
    STRONG_SECRET.slice(0, -1),
    ` ${STRONG_SECRET}`,
    STRONG_SECRET.toLowerCase(),
  ]
  for (const supplied of rejected) {
    installScenario()
    const headers = authorizedHeaders()
    if (supplied === undefined) delete headers['x-billing-recovery-secret']
    else headers['x-billing-recovery-secret'] = supplied
    const { response, recorder, body } = await enabled(() => post({ headers }))
    assert.equal(response.status, 401, JSON.stringify(supplied))
    assert.equal(response.headers.get('cache-control'), 'no-store')
    assert.deepEqual(body, { error: 'Recovery second factor is required' })
    assert.ok(recorder.reads.includes('x-billing-recovery-secret'))
    // No ledger mutation, and the body was never even read.
    assert.equal(recorder.textReads(), 0)
    assert.deepEqual(scenario.resolutions, [])
  }
})

// ---------------------------------------------------------------------------
// The input ladder proper
// ---------------------------------------------------------------------------

test('a non-JSON content type is a 415 before the body is read', { skip }, async () => {
  for (const contentType of [
    undefined, '', 'text/plain', 'application/x-www-form-urlencoded',
    'multipart/form-data; boundary=x', 'text/json', 'json',
  ]) {
    installScenario()
    const headers = authorizedHeaders()
    if (contentType === undefined) delete headers['content-type']
    else headers['content-type'] = contentType
    const { response, recorder, body } = await enabled(() => post({ headers }))
    assert.equal(response.status, 415, JSON.stringify(contentType))
    assert.deepEqual(body, { error: 'Content-Type must be application/json' })
    assert.equal(recorder.textReads(), 0)
  }

  // Accepted spellings: the check is case-insensitive and tolerates parameters.
  // MEASURED LAXNESS, pinned rather than assumed: the predicate is
  // `startsWith('application/json')`, so 'application/jsonl' and
  // 'application/json-seq' are also accepted. Harmless here (the body is still
  // JSON.parse'd and every field is validated), but it is not the strict
  // equality a reader of the route might assume, so it is recorded.
  for (const contentType of [
    'application/json', 'APPLICATION/JSON', 'application/json; charset=utf-8',
    'application/jsonl', 'application/json-seq',
  ]) {
    installScenario()
    const { response } = await enabled(() => post({
      headers: authorizedHeaders({ 'content-type': contentType }),
    }))
    assert.equal(response.status, 200, contentType)
  }
})

test('both 413 limits fire independently of each other', { skip }, async () => {
  // (a) A declared Content-Length over 4096 is refused WITHOUT reading the body,
  // so a lying-large declaration cannot be used to make the server buffer.
  installScenario()
  const declared = await enabled(() => post({
    headers: authorizedHeaders({ 'content-length': '4097' }),
    body: JSON.stringify(VALID_BODY),
  }))
  assert.equal(declared.response.status, 413)
  assert.deepEqual(declared.body, { error: 'Request body is too large' })
  assert.equal(declared.recorder.textReads(), 0, 'a declared oversize must not be read')

  // (b) The byte check catches a body that LIED about its length (absent or
  // understated Content-Length) — this is the one that actually protects the
  // parser, and it must not be reachable only through the header check.
  installScenario()
  const oversizeBody = JSON.stringify({ ...VALID_BODY, note: 'x'.repeat(5000) })
  assert.ok(Buffer.byteLength(oversizeBody, 'utf8') > 4096)
  const undeclared = await enabled(() => post({
    headers: authorizedHeaders(),
    body: oversizeBody,
  }))
  assert.equal(undeclared.response.status, 413)
  assert.equal(undeclared.recorder.textReads(), 1)
  assert.deepEqual(scenario.resolutions, [])

  // (c) Understated Content-Length: 10 declared, 5000 sent.
  installScenario()
  const understated = await enabled(() => post({
    headers: authorizedHeaders({ 'content-length': '10' }),
    body: oversizeBody,
  }))
  assert.equal(understated.response.status, 413)

  // (d) The boundary is not off by one: exactly 4096 bytes is accepted. The
  // padding goes in an IGNORED key, because `note` has its own 480-char ceiling
  // and would otherwise answer 400 before the size check could be observed.
  installScenario()
  const shell = JSON.stringify({ ...VALID_BODY, padding: '' })
  const exact = JSON.stringify({
    ...VALID_BODY,
    padding: 'y'.repeat(4096 - Buffer.byteLength(shell, 'utf8')),
  })
  assert.equal(Buffer.byteLength(exact, 'utf8'), 4096)
  const atLimit = await enabled(() => post({
    headers: authorizedHeaders({ 'content-length': '4096' }),
    body: exact,
  }))
  assert.equal(atLimit.response.status, 200)

  // One byte over is refused, so 4096 is genuinely the boundary.
  installScenario()
  const overByOne = JSON.stringify({
    ...VALID_BODY,
    padding: 'y'.repeat(4097 - Buffer.byteLength(shell, 'utf8')),
  })
  assert.equal(Buffer.byteLength(overByOne, 'utf8'), 4097)
  assert.equal((await enabled(() => post({
    headers: authorizedHeaders(), body: overByOne,
  }))).response.status, 413)

  // (e) The limit is measured in BYTES, not characters: a 2-byte-per-character
  // payload that is well under 4096 CHARACTERS is still refused.
  installScenario()
  const multibyte = JSON.stringify({ ...VALID_BODY, padding: 'é'.repeat(2100) })
  assert.ok(multibyte.length < 4096, 'the fixture must be under 4096 characters')
  assert.ok(Buffer.byteLength(multibyte, 'utf8') > 4096)
  const byteLimited = await enabled(() => post({ headers: authorizedHeaders(), body: multibyte }))
  assert.equal(byteLimited.response.status, 413)
})

test('four distinct 400 shapes are all refused with no ledger mutation', { skip }, async () => {
  const cases = [
    ['unparseable JSON', '{not json', 'Invalid JSON body'],
    ['empty body', '', 'Invalid JSON body'],
    ['JSON null', 'null', 'Invalid manual recovery request'],
    ['JSON array', '[{"entry_id":1}]', 'Invalid manual recovery request'],
    ['JSON scalar', '"reported"', 'Invalid manual recovery request'],
    ['missing entry_id', JSON.stringify({ resolution: 'reported', note: VALID_BODY.note }), 'Invalid manual recovery request'],
    ['zero entry_id', JSON.stringify({ ...VALID_BODY, entry_id: 0 }), 'Invalid manual recovery request'],
    ['negative entry_id', JSON.stringify({ ...VALID_BODY, entry_id: -1 }), 'Invalid manual recovery request'],
    ['fractional entry_id', JSON.stringify({ ...VALID_BODY, entry_id: 4.5 }), 'Invalid manual recovery request'],
    ['unsafe entry_id', JSON.stringify({ ...VALID_BODY, entry_id: 9007199254740993 }), 'Invalid manual recovery request'],
    ['non-numeric entry_id', JSON.stringify({ ...VALID_BODY, entry_id: 'abc' }), 'Invalid manual recovery request'],
    ['unknown resolution', JSON.stringify({ ...VALID_BODY, resolution: 'resolved' }), 'Invalid manual recovery request'],
    ['capped resolution', JSON.stringify({ ...VALID_BODY, resolution: 'capped' }), 'Invalid manual recovery request'],
    ['missing resolution', JSON.stringify({ entry_id: 41, note: VALID_BODY.note }), 'Invalid manual recovery request'],
    ['short note', JSON.stringify({ ...VALID_BODY, note: 'too short' }), 'Invalid manual recovery request'],
    ['whitespace note', JSON.stringify({ ...VALID_BODY, note: '            ' }), 'Invalid manual recovery request'],
    ['non-string note', JSON.stringify({ ...VALID_BODY, note: 12345678901234 }), 'Invalid manual recovery request'],
    ['overlong note', JSON.stringify({ ...VALID_BODY, note: 'n'.repeat(481) }), 'Invalid manual recovery request'],
  ]
  for (const [label, body, message] of cases) {
    installScenario()
    const result = await enabled(() => post({
      headers: authorizedHeaders({ 'content-length': String(Buffer.byteLength(body, 'utf8')) }),
      body,
    }))
    assert.equal(result.response.status, 400, label)
    assert.deepEqual(result.body, { error: message }, label)
    assert.deepEqual(scenario.resolutions, [], label)
  }

  // The accepted resolutions, for contrast — all three reach the RPC.
  for (const resolution of ['reported', 'dead', 'pending']) {
    installScenario()
    const result = await enabled(() => post({
      headers: authorizedHeaders(),
      body: JSON.stringify({ ...VALID_BODY, resolution }),
    }))
    assert.equal(result.response.status, 200, resolution)
    assert.equal(scenario.resolutions.length, 1)
    assert.equal(scenario.resolutions[0].resolution, resolution)
  }
})

test('the accepted request is scoped by the server-derived company and echoes a safe request id', { skip }, async () => {
  installScenario()
  const accepted = await enabled(() => post({
    headers: authorizedHeaders({ 'x-request-id': 'operator-ticket-12345' }),
    body: JSON.stringify({ ...VALID_BODY, note: '  padded operator note  ' }),
  }))
  assert.equal(accepted.response.status, 200)
  assert.equal(accepted.response.headers.get('x-request-id'), 'operator-ticket-12345')
  assert.equal(accepted.response.headers.get('cache-control'), 'no-store')
  assert.deepEqual(accepted.body, {
    resolved: true,
    entry_id: 41,
    resolution: 'reported',
    audit_id: 907,
    manual_only: true,
  })
  // The organization is the SERVER-derived active company, never anything from
  // the body; the note is trimmed; the actor is the authenticated user.
  assert.deepEqual(scenario.resolutions, [{
    actorUserId: USER_ID,
    expectedOrganizationId: ORGANIZATION_ID,
    entryId: 41,
    resolution: 'reported',
    note: 'padded operator note',
    requestId: 'operator-ticket-12345',
  }])

  // A malformed or oversized inbound id is replaced by a generated UUID rather
  // than reflected, so the header cannot be used to inject log content.
  for (const supplied of ['short', 'has space', 'a'.repeat(129), '<script>abc</script>', '']) {
    installScenario()
    const result = await enabled(() => post({
      headers: authorizedHeaders({ 'x-request-id': supplied }),
    }))
    assert.equal(result.response.status, 200, JSON.stringify(supplied))
    const echoed = result.response.headers.get('x-request-id')
    assert.notEqual(echoed, supplied, JSON.stringify(supplied))
    assert.match(echoed, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    assert.equal(scenario.resolutions[0].requestId, echoed)
  }
})

test('an ineligible entry is a 409 and an authorization change is a 403, both with the request id', { skip }, async () => {
  for (const [code, status] of [
    ['not_found', 409],
    ['ineligible', 409],
    ['already_resolved', 409],
    ['forbidden', 403],
    ['active_company_changed', 403],
  ]) {
    installScenario({ resolution: { ok: false, code, auditId: null } })
    const result = await enabled(() => post({
      headers: authorizedHeaders({ 'x-request-id': 'operator-ticket-12345' }),
    }))
    assert.equal(result.response.status, status, code)
    assert.equal(result.response.headers.get('x-request-id'), 'operator-ticket-12345', code)
    assert.equal(result.response.headers.get('cache-control'), 'no-store', code)
    assert.deepEqual(result.body, {
      error: status === 403
        ? 'Billing permission is required for the active company'
        : 'Ledger entry is not eligible for manual recovery',
    }, code)
  }
})

test('the launch gate is restored for the rest of the process', () => {
  assert.equal(process.env.BREVITAS_BILLING_ENABLED, originalGate)
})
