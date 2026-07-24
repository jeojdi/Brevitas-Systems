import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { unsignedCiTestClaim } from '../scripts/ci/write-unsigned-ci-claim.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const SHA = 'a'.repeat(40)

test('unsigned CI claim identifies its limitations and contains no branch or path', () => {
  const record = unsignedCiTestClaim({
    GITHUB_SHA: SHA,
    GITHUB_RUN_ID: '1234',
    GITHUB_RUN_ATTEMPT: '2',
    GITHUB_REF_NAME: 'main',
    GITHUB_WORKSPACE: '/secret/checkout',
  }, () => new Date('2026-07-20T20:15:18Z'))
  assert.deepEqual(record, {
    schema_version: 1,
    claim_type: 'unsigned_ci_test_claim',
    cryptographic_attestation: false,
    deployment_verified: false,
    commit_sha: SHA,
    tested_at: '2026-07-20T20:15:18.000Z',
    workflow_run_id: '1234',
    workflow_run_attempt: '2',
  })
  assert.doesNotMatch(JSON.stringify(record), /main|secret|checkout/)
  assert.throws(() => unsignedCiTestClaim({ GITHUB_SHA: 'main' }), /full immutable/)
})

test('repository build wiring injects a commit label and emits only a canonical-main unsigned claim', () => {
  const dockerfile = read('Dockerfile')
  const workflow = read('.github/workflows/security.yml')
  assert.match(dockerfile, /ARG BREVITAS_BUILD_SHA=""/)
  assert.match(dockerfile, /org\.opencontainers\.image\.revision/)
  assert.match(workflow, /--build-arg BREVITAS_BUILD_SHA="\$GITHUB_SHA"/)
  assert.match(workflow, /unsigned-ci-test-claim-\$\{\{ github\.sha \}\}/)
  assert.match(workflow, /write-unsigned-ci-claim\.mjs/)
  assert.match(workflow, /github\.event_name == 'push'/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
  assert.match(workflow, /github\.repository == 'jeojdi\/Brevitas-Systems'/)
  assert.match(workflow, /github\.event\.repository\.fork == false/)
  assert.doesNotMatch(workflow, /tested commit provenance|release-provenance/i)
  assert.match(workflow, /actions\/upload-artifact@[0-9a-f]{40}/)
})

test('Vercel exposes a static safe build contract and rejects production ambiguity', () => {
  const route = read('src/app/api/version/route.ts')
  const provenance = read('src/lib/build-provenance.ts')
  assert.match(route, /dynamic = "force-static"/)
  assert.match(route, /service: "dashboard"/)
  assert.match(provenance, /VERCEL_GIT_COMMIT_SHA/)
  assert.match(provenance, /Conflicting build commit identities/)
  assert.doesNotMatch(route, /branch|ref_name|workspace/i)
})
