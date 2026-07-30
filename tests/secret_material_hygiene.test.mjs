import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

// api/.secret_key and api/brevitas.db were committed once and are still reachable from
// main, and the secret scan cannot see them: security.yml runs TruffleHog with
// --only-verified, which a raw base64 symmetric key and a SQLite blob can never satisfy.
// A filename rule is the only gate that would have fired, so this is that gate.
const FORBIDDEN = [
  /(?:^|\/)\.?[^/]*\.secret_key$/,
  /(?:^|\/)[^/]*\.db$/,
  /(?:^|\/)[^/]*\.sqlite3?$/,
  /(?:^|\/)[^/]*\.pem$/,
]

function trackedFiles() {
  const result = spawnSync('git', ['ls-files', '-z'], { cwd: root, encoding: 'utf8' })
  assert.equal(result.status, 0, `git ls-files failed: ${result.stderr || result.error}`)
  return result.stdout.split('\0').filter(Boolean)
}

test('no key material or local database is tracked in the repository', () => {
  const offenders = trackedFiles().filter(path => FORBIDDEN.some(pattern => pattern.test(path)))
  assert.deepEqual(offenders, [],
    'tracked secret material or local database; history cannot be un-published once pushed')
})

test('ignore rules cover renamed key files and local databases, not just two exact paths', () => {
  const gitignore = read('.gitignore')
  for (const pattern of ['.secret_key', '*.secret_key', '*.db', '*.sqlite', '*.sqlite3']) {
    assert.ok(gitignore.split('\n').includes(pattern), `.gitignore is missing ${pattern}`)
  }
  const dockerignore = read('.dockerignore')
  for (const pattern of ['api/.secret_key', 'api/*.db', '**/*.sqlite', '**/*.sqlite3', '*.pem', '*.key']) {
    assert.ok(dockerignore.split('\n').includes(pattern), `.dockerignore is missing ${pattern}`)
  }
})

test('the exposed Fernet key is documented as burned with a required response', () => {
  const doc = read('docs/CREDENTIAL_SECURITY.md')
  assert.match(doc, /## Retired and exposed key material/)
  assert.match(doc, /api\/\.secret_key/)
  assert.match(doc, /burned/)
  assert.match(doc, /bvt-envelope:v1:/)
  // The legacy decrypt bridge is deliberately retained; it is unrelated to this key.
  assert.match(doc, /LegacyFernetDecryptor.+stay/s)
  // The API must still never read a secret file.
  assert.match(doc, /The API must never read or create `api\/\.secret_key`/)
})
