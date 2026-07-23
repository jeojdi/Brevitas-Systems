import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

test('homepage quick starts continue into full BVX onboarding', async () => {
  const components = await readFile(new URL('../public/components.jsx', import.meta.url), 'utf8')
  const defaults = components.match(/const DEFAULT_INSTALL_COMMANDS = \[[^]*?\n\];/)?.[0]

  assert.ok(defaults, 'DEFAULT_INSTALL_COMMANDS must be present')
  assert.match(defaults, /brew install Brevitas-ai\/brevitas\/bvx && bvx install/)
  assert.match(
    defaults,
    /irm https:\/\/raw\.githubusercontent\.com\/Brevitas-ai\/brevitas\/main\/install\.ps1 \| iex; if \(\$\?\) \{ bvx install \}/,
  )
  assert.doesNotMatch(defaults, /bvx login/)
})
