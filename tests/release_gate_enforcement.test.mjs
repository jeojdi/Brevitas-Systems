import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

function jobBlock(workflow, name) {
  const jobs = workflow.slice(workflow.indexOf('\njobs:'))
  const start = jobs.indexOf(`\n  ${name}:`)
  assert.notEqual(start, -1, `release.yml has no ${name} job`)
  const rest = jobs.slice(start + 1)
  const next = rest.search(/\n {2}[a-z][a-z0-9-]*:\n/)
  return next === -1 ? rest : rest.slice(0, next)
}

test('an untrusted or ambiguous release invocation fails instead of skipping', () => {
  const workflow = read('.github/workflows/release.yml')
  const guard = jobBlock(workflow, 'guard')

  // A job-level `if:` that is not met SKIPS the job, and every stage below is
  // needs:-chained to guard, so the whole release chain skipped and GitHub reported
  // the run as a success. The contract must be asserted inside the job.
  assert.doesNotMatch(guard, /^\s+if:/m,
    'guard is gated by a job-level if:, so an unmet condition skips the chain green')
  for (const assertion of [
    /test "\$IS_FORK" = "false"/,
    /test "\$REPOSITORY" = "jeojdi\/Brevitas-Systems"/,
    /test "\$REF_NAME" = "refs\/heads\/main"/,
    /test "\$TARGET" = "staging" \|\| test "\$TARGET" = "production"/,
  ]) {
    assert.match(guard, assertion)
  }

  // The ordered chain is what makes a failure stop the release; keep it unbroken.
  const chain = [
    ['migrations-check', 'guard'],
    ['schema-drift', 'migrations-check'],
    ['operational-readiness', 'schema-drift'],
    ['preflight', 'operational-readiness'],
    ['staging-smoke', 'preflight'],
  ]
  for (const [job, dependency] of chain) {
    assert.match(jobBlock(workflow, job), new RegExp(`needs: ${dependency}\\b`),
      `${job} is no longer chained to ${dependency}`)
  }
})

test('the release workflow and gate doc state that nothing here blocks a deploy yet', () => {
  const workflow = read('.github/workflows/release.yml')
  const doc = read('docs/enterprise/OPERATIONAL_READINESS_GATE.md')

  // Vercel and Railway both deploy from a push to main and consult no GitHub check,
  // so a red run here stops nothing until the platform configuration is done. Saying
  // otherwise is the compliance defect.
  assert.match(workflow, /does NOT block a deployment/)
  assert.match(workflow, /OPERATIONAL_READINESS_GATE\.md/)

  assert.match(doc, /## Making this gate mandatory/)
  assert.match(doc, /\*\*OPEN CONTROL GAP\.\*\*/)
  assert.match(doc, /Disable Vercel Git auto-deploy/)
  assert.match(doc, /Disable Railway's deploy-on-push trigger/)
  assert.match(doc, /needs: preflight/)
  assert.match(doc, /Release orchestrator/)
  assert.doesNotMatch(doc, /The manual workflow alone blocks/)
})
