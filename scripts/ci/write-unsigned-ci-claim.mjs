import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const fullCommitSha = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/

export function unsignedCiTestClaim(environment = process.env, now = () => new Date()) {
  const commitSha = String(environment.GITHUB_SHA || '').trim().toLowerCase()
  if (!fullCommitSha.test(commitSha)) {
    throw new Error('GITHUB_SHA must be a full immutable Git commit SHA')
  }
  const testedAt = now().toISOString()
  const runId = String(environment.GITHUB_RUN_ID || '').trim()
  const runAttempt = String(environment.GITHUB_RUN_ATTEMPT || '').trim()
  if (runId && !/^\d+$/.test(runId)) throw new Error('GITHUB_RUN_ID must be numeric')
  if (runAttempt && !/^\d+$/.test(runAttempt)) {
    throw new Error('GITHUB_RUN_ATTEMPT must be numeric')
  }
  return {
    schema_version: 1,
    claim_type: 'unsigned_ci_test_claim',
    cryptographic_attestation: false,
    deployment_verified: false,
    commit_sha: commitSha,
    tested_at: testedAt,
    ...(runId ? { workflow_run_id: runId } : {}),
    ...(runAttempt ? { workflow_run_attempt: runAttempt } : {}),
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2)
    if (args.length !== 2 || args[0] !== '--output' || !args[1]) {
      throw new Error('Usage: node scripts/ci/write-unsigned-ci-claim.mjs --output FILE')
    }
    writeFileSync(resolve(args[1]), `${JSON.stringify(unsignedCiTestClaim(), null, 2)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    })
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
