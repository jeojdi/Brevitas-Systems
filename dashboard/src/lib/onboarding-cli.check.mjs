import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  BVX_COMMANDS,
  BVX_PLATFORMS,
  nextOnboardingStep,
  ONBOARDING_STEP,
} from './onboarding-cli.js'

const component = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

test('onboarding has a real three-step workspace, connection, and verification flow', async () => {
  assert.equal(nextOnboardingStep(ONBOARDING_STEP.WORKSPACE), ONBOARDING_STEP.CONNECT)
  assert.equal(nextOnboardingStep(ONBOARDING_STEP.CONNECT), ONBOARDING_STEP.VERIFY)
  assert.equal(nextOnboardingStep(ONBOARDING_STEP.VERIFY), ONBOARDING_STEP.VERIFY)

  const [onboarding, app] = await Promise.all([
    component('OnboardingWorkspaceChoice'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])
  assert.match(onboarding, /Step 1 of 3 · choose your workspace/)
  assert.match(onboarding, /Step 2 of 3 · connect a tool/)
  assert.match(onboarding, /Step 3 of 3 · verify one request/)
  assert.match(onboarding, /<InstallCommand phase="setup"/)
  assert.match(onboarding, /<InstallCommand phase="verify"/)
  assert.match(app, /onFinish=\{finishWorkspaceSetup\}/)
  assert.match(app, /needsOnboarding: context\.onboarding\.status !== 'complete'/)
  assert.match(app, /initialWorkspaceCreated=\{companyContext\.workspaceCreated\}/)
  assert.match(app, /deviceCode && companyContext\.activeCompanyId/)
  assert.match(onboarding, /Check for verified request/)
  assert.doesNotMatch(onboarding, /Finish this later|I verified a proxied request/)
})

test('platform install commands match the distributed BVX installation paths', () => {
  assert.deepEqual(BVX_PLATFORMS.map(platform => platform.label), ['macOS', 'Linux', 'Windows'])
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'macos').installCommand,
    'brew install Brevitas-ai/brevitas/bvx')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'linux').installCommand,
    'brew install Brevitas-ai/brevitas/bvx')
  assert.equal(BVX_PLATFORMS.find(platform => platform.id === 'windows').installCommand,
    'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex')
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
  assert.match(install, /bvx install[^]*includes browser authentication/)
  assert.match(install, /Requests proxied/)
  assert.match(overview, /<InstallCommand phase="all"/)
  assert.match(docs, /Requests proxied/)
  assert.doesNotMatch(install, /&& bvx login/)
})
