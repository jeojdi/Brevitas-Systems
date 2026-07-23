export const ONBOARDING_STEP = Object.freeze({
  WORKSPACE: 1,
  CONNECT: 2,
  VERIFY: 3,
})

export const nextOnboardingStep = currentStep => {
  if (currentStep === ONBOARDING_STEP.WORKSPACE) return ONBOARDING_STEP.CONNECT
  if (currentStep === ONBOARDING_STEP.CONNECT) return ONBOARDING_STEP.VERIFY
  return ONBOARDING_STEP.VERIFY
}

export const BVX_PLATFORMS = Object.freeze([
  {
    id: 'macos',
    label: 'macOS',
    prompt: '$',
    language: 'bash',
    installCommand: 'brew install Brevitas-ai/brevitas/bvx',
    quickStartCommand: 'brew install Brevitas-ai/brevitas/bvx && bvx install',
    note: 'Homebrew installs the supported Python dependency with BVX.',
  },
  {
    id: 'linux',
    label: 'Linux',
    prompt: '$',
    language: 'bash',
    installCommand: 'brew install Brevitas-ai/brevitas/bvx',
    quickStartCommand: 'brew install Brevitas-ai/brevitas/bvx && bvx install',
    note: 'This uses Homebrew on Linux and installs the supported Python dependency with BVX.',
  },
  {
    id: 'windows',
    label: 'Windows',
    prompt: 'PS>',
    language: 'powershell',
    installCommand: 'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex',
    quickStartCommand: 'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex; if ($?) { bvx install }',
    note: 'No GitHub account or API token is required. Install Python 3.13 or newer first, then open a new PowerShell window after this installer updates PATH.',
  },
])

export const BVX_COMMANDS = Object.freeze({
  version: 'bvx version',
  setup: 'bvx install',
  status: 'bvx status',
  diagnose: 'bvx doctor',
  verifyRequest: 'bvx stats',
})
