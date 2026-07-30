import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
// Comments quote the old, unsafe invocations on purpose; assert against code only.
const code = path => read(path).split('\n').filter(line => !/^\s*#/.test(line)).join('\n')

test('the migration applier keeps the production DB URI out of psql argv', () => {
  const applier = read('scripts/db/apply-migrations.sh')

  // /proc/<pid>/cmdline and `ps auxww` are readable by every local process, and this
  // is the only applier for the whole chain (remote Supabase migration history is
  // empty, so `supabase db push` is unusable), so it ran on every schema change.
  assert.doesNotMatch(code('scripts/db/apply-migrations.sh'), /psql "\$DB_URL"/)
  assert.match(applier, /source "\$SCRIPT_DIR\/common\.sh"/)
  assert.match(applier, /dr_database_exec "\$DB_URL" psql -v ON_ERROR_STOP=1 -qtAX -c "\$1"/)
  assert.match(applier, /dr_database_exec "\$DB_URL" psql -v ON_ERROR_STOP=1 -qX -f "\$tmp"/)
  assert.match(applier, /trap 'rm -f "\$\{tmp:-\}"' EXIT/)
  // The header claim must describe argv too, not only stdout.
  assert.match(applier, /never enters psql's argv/)

  // The primitive it now depends on must keep reading the credential from a
  // descriptor and must keep exec'ing in place, or the -qtAX drift comparison breaks.
  const common = read('scripts/dr/common.sh')
  assert.match(common, /--database-url-fd 3 --connect-timeout 10 -- "\$@" 3<<< "\$database_url"/)
  assert.match(read('scripts/dr/libpq-exec.py'), /os\.execvpe\(command\[0\], command, environment\)/)
})

test('the production env script cannot leave secrets in /tmp or write an empty credential', () => {
  const script = read('scripts/release/production-env-setup.sh')
  const body = code('scripts/release/production-env-setup.sh')

  // Both temp files hold production secrets: the Supabase service-role key, then the
  // GCP KMS service-account private key that decrypts every customer provider key.
  assert.match(script, /^set -euo pipefail$/m)
  assert.match(script, /^umask 077$/m)
  assert.match(script, /^trap 'rm -f "\$\{TMP_ENV:-\}" "\$\{SA_KEY:-\}"' EXIT INT TERM$/m)

  // `grep | head -1 | cut` returns an empty string for a renamed Vercel variable, and
  // `--set "SUPABASE_SERVICE_ROLE_KEY="` wipes the production API's DB credential.
  assert.doesNotMatch(body, /head -1 \| cut/)
  assert.doesNotMatch(body, /--set "SUPABASE_SERVICE_ROLE_KEY=\$\(getvar/)
  for (const name of [
    'NEXT_PUBLIC_SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'NEXT_PUBLIC_SUPABASE_ANON_KEY',
  ]) {
    assert.match(script, new RegExp(`\\|\\| fail_missing ${name}\\b`), `${name} is unasserted`)
    assert.match(script, new RegExp(`\\|\\| fail_shape ${name}\\b`), `${name} is unshaped`)
  }

  // The SA private key goes through the CLI's file form when it has one; the argv
  // fallback must stay on the else branch and must announce the residual exposure.
  const kmsPhase = body.slice(body.indexOf('==> 5/5'))
  assert.match(kmsPhase, /if railway variables --help[^\n]*grep -q -- '--set-from-file'; then/)
  assert.match(kmsPhase, /--set-from-file "GCP_SA_KEY_JSON=\$SA_KEY"/)
  const fallback = kmsPhase.slice(kmsPhase.indexOf('\nelse\n'))
  assert.match(fallback, /--set "GCP_SA_KEY_JSON=\$\(cat "\$SA_KEY"\)"/)
  assert.match(fallback, /briefly/)

  // zsh does not run the EXIT trap when `set -e` aborts inside a function, and
  // `if ! fn` suppresses err_exit for the whole body, so the credential-bearing
  // phase must stay inline or a failure leaves the private key in /tmp.
  assert.doesNotMatch(kmsPhase, /^\s*\w+\(\) \{/m)
  // `local path=` in zsh rebinds $PATH, which broke even the trap's own `rm`.
  assert.doesNotMatch(body, /local [^\n]*\bpath=/)
})
