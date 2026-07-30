import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

// public/privacy.html is published and effective-dated, and docs/compliance/DPA_DRAFT.md
// warrants that only vendors registered in SUBPROCESSORS_DRAFT.md are used. A vendor named
// in the public policy but absent from the register makes that warranty false on its face,
// which is why this direction is asserted and not the reverse: the register may legitimately
// carry not-yet-enabled placeholders the public document does not mention.
const PUBLICLY_NAMED_VENDORS = ['PostHog', 'Supabase', 'Vercel', 'Railway', 'IONOS', 'Mailgun']

test('every vendor named in the published privacy policy is in the subprocessor register', () => {
  const privacy = read('public/privacy.html')
  const register = read('docs/compliance/SUBPROCESSORS_DRAFT.md')
  for (const vendor of PUBLICLY_NAMED_VENDORS) {
    assert.ok(privacy.includes(vendor), `privacy.html no longer names ${vendor}`)
    assert.ok(register.includes(vendor), `subprocessor register omits ${vendor}`)
  }
})

test('the register describes wired processors rather than unselected placeholders', () => {
  const register = read('docs/compliance/SUBPROCESSORS_DRAFT.md')

  // Google Cloud KMS is the production credential-envelope adapter, so it cannot be
  // carried as an unselected provider.
  assert.match(register, /\| Google Cloud KMS \|/)
  assert.match(register, /brevitas\.security\.google_cloud_kms/)
  assert.doesNotMatch(register, /\| Backup object store\/KMS \|/)

  // The content-free claim applies to the OpenTelemetry monitoring provider only.
  // PostHog receives a pseudonymous account identifier and masked DOM recordings.
  assert.match(register, /session replay/i)
  assert.match(register, /Pseudonymous account identifier/i)
  assert.match(register, /PostHog is a separate\s+processor and is not content-free/)

  // Rows for vendors already running must not read as pre-enablement placeholders.
  for (const vendor of ['PostHog', 'IONOS', 'Mailgun', 'Google Cloud KMS']) {
    const row = register.split('\n').find(line => line.startsWith(`| ${vendor} |`))
    assert.ok(row, `no register row for ${vendor}`)
    assert.match(row, /In production/, `${vendor} row understates that it is already enabled`)
  }
})
