import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { BVX_COMMANDS, BVX_PLATFORMS } from './onboarding-cli.js'

const component = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

test('only a missing workspace holds the dashboard back', async () => {
  const [onboarding, app] = await Promise.all([
    component('OnboardingWorkspaceChoice'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])

  // Entry is gated on membership alone. Gating on onboarding status is what kept a
  // new account out of the dashboard until a CLI install and a proxied request
  // both landed.
  assert.match(app, /needsOnboarding: context\.companies\.length === 0/)
  assert.doesNotMatch(app, /needsOnboarding: context\.onboarding\.status !== 'complete'/)
  assert.match(app, /setupComplete: context\.onboarding\.status === 'complete'/)
  assert.match(app, /deviceCode && companyContext\.activeCompanyId/)

  // The blocking wizard steps are gone, along with their live-verification poll.
  assert.doesNotMatch(onboarding, /Step \d of 3/)
  assert.doesNotMatch(onboarding, /InstallCommand/)
  assert.doesNotMatch(onboarding, /checkVerification|setInterval/)
  assert.doesNotMatch(onboarding, /onCheck|onFinish/)
  assert.doesNotMatch(app, /onFinish=\{finishWorkspaceSetup\}/)
})

test('the dashboard asks for the remaining setup instead of gating on it', async () => {
  const [banner, app] = await Promise.all([
    component('SetupBanner'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])

  // Same two facts the wizard demanded, checked the same way, now non-blocking.
  assert.match(banner, /cliConnected/)
  assert.match(banner, /proxiedRequestObserved/)
  assert.match(banner, /First request observed/)
  // Completion is recorded when both land, which is what removes the bar.
  assert.match(banner, /onComplete\?\.\(\)/)

  assert.match(app, /!companyContext\.setupComplete && \(/)
  assert.match(app, /onCheck=\{checkWorkspaceSetup\}/)
  assert.match(app, /onComplete=\{finishWorkspaceSetup\}/)
  // The button has to reach the tab that actually carries the install commands.
  assert.match(app, /onOpenSetup=\{\(\) => setActiveTab\('Connect'\)\}/)
  assert.match(app, /activeTab === 'Connect' && <ConnectionPage/)
})

test('platform install commands match the distributed BVX installation paths', () => {
  assert.deepEqual(BVX_PLATFORMS.map(platform => platform.label), ['macOS', 'Linux', 'Windows'])
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'macos').installCommand,
    'brew install Brevitas-ai/brevitas/bvx')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'linux').installCommand,
    'brew install Brevitas-ai/brevitas/bvx')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'windows').installCommand,
    'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex')
  assert.match(BVX_PLATFORMS.find(platform => platform.id === 'windows').note,
    /No GitHub account or API token is required/)
  // The quick start signs in explicitly (login first, so install reuses the
  // stored key instead of prompting a second time) on macOS and Windows.
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'macos').quickStartCommand,
    'brew install Brevitas-ai/brevitas/bvx && bvx login && bvx install')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'windows').quickStartCommand,
    'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex; '
    + 'if ($?) { bvx login; if ($?) { bvx install } }')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'linux').quickStartCommand,
    'brew install Brevitas-ai/brevitas/bvx && bvx install')
})

test('setup authenticates and configures through install, then proves a real proxied request', async () => {
  assert.deepEqual(BVX_COMMANDS, {
    version: 'bvx version',
    setup: 'bvx install',
    status: 'bvx status',
    diagnose: 'bvx doctor',
    verifyRequest: 'bvx stats',
  })

  const [install, overview, docs] = await Promise.all([
    component('InstallCommand'),
    component('Overview'),
    component('Docs'),
  ])
  assert.match(install, /bvx install[^]*includes browser\s+authentication/)
  assert.match(install, /Requests proxied/)
  assert.match(overview, /<InstallCommand phase="all"/)
  assert.match(docs, /Requests proxied/)
  assert.match(install, /bvx login[^]*authorizes this device/)
  assert.match(install, /One command connects your tools/)
  assert.match(install, /No API key copying is required/)
})
