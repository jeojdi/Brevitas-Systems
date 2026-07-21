import { useId, useState } from 'react'
import InstallCommand from './InstallCommand.jsx'
import { nextOnboardingStep, ONBOARDING_STEP } from '../lib/onboarding-cli.js'
import {
  normalizeWorkspaceSelection,
  WORKSPACE_NAME_MAX_LENGTH,
  WORKSPACE_TYPE,
} from '../lib/onboarding-workspace.js'

const choices = [
  {
    value: WORKSPACE_TYPE.PERSONAL,
    eyebrow: 'For individual use',
    title: 'Personal workspace',
    description: 'A private workspace for your own projects, usage, API keys, and billing.',
    steps: [
      'Only your account is added at first.',
      'Connect your app or the BVX CLI.',
      'Invite teammates later without moving your work.',
    ],
  },
  {
    value: WORKSPACE_TYPE.COMPANY,
    eyebrow: 'For teams and enterprise',
    title: 'Company workspace',
    description: 'A shared company boundary with roles for people and separate credentials for production systems.',
    steps: [
      'You become the company owner.',
      'Invite people by email and assign their roles.',
      'Create a service key for your production backend.',
    ],
  },
]

export default function OnboardingWorkspaceChoice({
  initialWorkspaceCreated = false,
  initialWorkspaceType = '',
  initialWorkspaceName = '',
  isSubmitting = false,
  errorMessage = '',
  onContinue,
  onBack,
  onFinish,
  onWorkspaceTypeChange,
  onWorkspaceNameChange,
}) {
  const headingId = useId()
  const helpId = useId()
  const errorId = useId()
  const fieldName = useId()
  const [workspaceType, setWorkspaceType] = useState(initialWorkspaceType)
  const [workspaceName, setWorkspaceName] = useState(initialWorkspaceName)
  const [step, setStep] = useState(
    initialWorkspaceCreated ? ONBOARDING_STEP.CONNECT : ONBOARDING_STEP.WORKSPACE,
  )
  const [validationError, setValidationError] = useState('')
  const [localSubmitting, setLocalSubmitting] = useState(false)
  const [finishing, setFinishing] = useState(false)
  const [finishError, setFinishError] = useState('')
  const busy = isSubmitting || localSubmitting
  const displayedError = errorMessage || validationError

  const chooseWorkspaceType = nextType => {
    setWorkspaceType(nextType)
    setValidationError('')
    onWorkspaceTypeChange?.(nextType)
  }

  const changeWorkspaceName = event => {
    const nextName = event.target.value
    setWorkspaceName(nextName)
    setValidationError('')
    onWorkspaceNameChange?.(nextName)
  }

  const submit = async event => {
    event.preventDefault()
    setValidationError('')

    let selection
    try {
      selection = normalizeWorkspaceSelection({ workspaceType, workspaceName })
    } catch (reason) {
      setValidationError(reason instanceof Error ? reason.message : 'Check your workspace details.')
      return
    }

    setLocalSubmitting(true)
    try {
      await onContinue?.(selection)
      setStep(current => nextOnboardingStep(current))
    } catch (reason) {
      setValidationError(reason instanceof Error ? reason.message : 'Workspace setup could not continue.')
    } finally {
      setLocalSubmitting(false)
    }
  }

  const finish = async () => {
    setFinishError('')
    setFinishing(true)
    try {
      await onFinish?.()
    } catch (reason) {
      setFinishError(reason instanceof Error ? reason.message : 'The dashboard could not finish onboarding.')
    } finally {
      setFinishing(false)
    }
  }

  if (step === ONBOARDING_STEP.CONNECT) {
    return (
      <section aria-labelledby={headingId} className="mx-auto w-full max-w-4xl">
        <header className="mx-auto mb-7 max-w-2xl text-center sm:mb-10">
          <p className="annotation mb-3 uppercase tracking-widest">Step 2 of 3 · connect a tool</p>
          <h1 id={headingId} className="font-serif text-4xl leading-tight text-brand-navy dark:text-brand-dark-navy sm:text-5xl">
            Put BVX on the request path.
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid sm:text-base">
            Your workspace is ready. Install the released BVX CLI and let its interactive setup authenticate and configure a local AI tool.
          </p>
        </header>
        <InstallCommand phase="setup" />
        <div className="mt-6 flex justify-end">
          <button
            type="button"
            onClick={() => setStep(current => nextOnboardingStep(current))}
            className="min-h-11 rounded-xl bg-brand-blue px-6 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90"
          >
            Continue to verification
          </button>
        </div>
      </section>
    )
  }

  if (step === ONBOARDING_STEP.VERIFY) {
    return (
      <section aria-labelledby={headingId} className="mx-auto w-full max-w-4xl">
        <header className="mx-auto mb-7 max-w-2xl text-center sm:mb-10">
          <p className="annotation mb-3 uppercase tracking-widest">Step 3 of 3 · verify one request</p>
          <h1 id={headingId} className="font-serif text-4xl leading-tight text-brand-navy dark:text-brand-dark-navy sm:text-5xl">
            Prove traffic reaches BVX.
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid sm:text-base">
            A healthy service is not enough. Run diagnostics and make one normal request from a configured tool. Brevitas will unlock the dashboard only after the server records that request through your workspace&apos;s BVX device key.
          </p>
        </header>
        <InstallCommand phase="verify" />
        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          <button
            type="button"
            onClick={() => setStep(ONBOARDING_STEP.CONNECT)}
            disabled={finishing}
            className="min-h-11 rounded-xl border border-brand-border px-5 py-3 text-sm font-medium text-brand-navy disabled:opacity-50 dark:border-brand-dark-border dark:text-brand-dark-navy"
          >
            Back to setup
          </button>
          <button
            type="button"
            onClick={finish}
            disabled={finishing}
            className="min-h-11 rounded-xl bg-brand-blue px-6 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {finishing ? 'Checking server evidence…' : 'Check for verified request'}
          </button>
        </div>
        {finishError && <p role="alert" className="mt-4 text-sm text-red-500">{finishError}</p>}
      </section>
    )
  }

  return (
    <section aria-labelledby={headingId} className="w-full max-w-5xl mx-auto">
      <header className="max-w-2xl mx-auto text-center mb-7 sm:mb-10">
        <p className="annotation tracking-widest uppercase mb-3">Step 1 of 3 · choose your workspace</p>
        <h1 id={headingId} className="font-serif text-4xl sm:text-5xl leading-tight text-brand-navy dark:text-brand-dark-navy">
          How will you use Brevitas?
        </h1>
        <p id={helpId} className="mt-3 text-sm sm:text-base leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid">
          A workspace controls who can access projects, usage, API keys, and billing. You can add a company workspace later.
        </p>
      </header>

      <form onSubmit={submit} aria-busy={busy} aria-describedby={`${helpId}${displayedError ? ` ${errorId}` : ''}`}>
        <fieldset disabled={busy}>
          <legend className="sr-only">Choose a workspace type</legend>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 sm:gap-5">
            {choices.map(choice => {
              const selected = workspaceType === choice.value
              const inputId = `${fieldName}-${choice.value}`
              return (
                <label
                  key={choice.value}
                  htmlFor={inputId}
                  className={`relative flex min-w-0 cursor-pointer flex-col rounded-2xl border p-5 sm:p-6 transition-colors ${
                    selected
                      ? 'border-brand-blue bg-brand-blue-dim/70 dark:bg-brand-dark-blue-dim'
                      : 'border-brand-border bg-white hover:border-brand-border-mid dark:border-brand-dark-border dark:bg-brand-dark-surface dark:hover:border-brand-dark-border-mid'
                  }`}
                >
                  <div className="flex items-start gap-4">
                    <input
                      id={inputId}
                      type="radio"
                      name={fieldName}
                      value={choice.value}
                      checked={selected}
                      onChange={() => chooseWorkspaceType(choice.value)}
                      className="mt-1 h-5 w-5 shrink-0 accent-brand-blue focus:ring-2 focus:ring-brand-blue focus:ring-offset-2 dark:focus:ring-offset-brand-dark-surface"
                    />
                    <div className="min-w-0">
                      <p className="annotation uppercase tracking-widest">{choice.eyebrow}</p>
                      <h2 className="mt-1 font-serif text-2xl sm:text-3xl text-brand-navy dark:text-brand-dark-navy">
                        {choice.title}
                      </h2>
                    </div>
                  </div>

                  <p className="mt-4 text-sm leading-relaxed text-brand-navy-mid dark:text-brand-dark-navy-mid">
                    {choice.description}
                  </p>
                  <div className="mt-5 border-t border-brand-border dark:border-brand-dark-border pt-4">
                    <p className="text-[11px] font-medium uppercase tracking-widest text-brand-muted dark:text-brand-dark-muted">
                      What happens next
                    </p>
                    <ol className="mt-3 space-y-2.5">
                      {choice.steps.map((step, index) => (
                        <li key={step} className="flex gap-3 text-xs sm:text-sm leading-relaxed text-brand-navy-mid dark:text-brand-dark-navy-mid">
                          <span aria-hidden="true" className="font-mono text-brand-blue">{index + 1}</span>
                          <span>{step}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                </label>
              )
            })}
          </div>
        </fieldset>

        {workspaceType && (
          <div className="mt-5 rounded-2xl border border-brand-border bg-white p-5 sm:p-6 dark:border-brand-dark-border dark:bg-brand-dark-surface">
            <label htmlFor={`${fieldName}-name`} className="block text-[11px] font-medium uppercase tracking-widest text-brand-muted dark:text-brand-dark-muted">
              {workspaceType === WORKSPACE_TYPE.COMPANY ? 'Company name' : 'Workspace name (optional)'}
            </label>
            <input
              id={`${fieldName}-name`}
              type="text"
              value={workspaceName}
              onChange={changeWorkspaceName}
              required={workspaceType === WORKSPACE_TYPE.COMPANY}
              maxLength={WORKSPACE_NAME_MAX_LENGTH}
              autoComplete={workspaceType === WORKSPACE_TYPE.COMPANY ? 'organization' : 'off'}
              placeholder={workspaceType === WORKSPACE_TYPE.COMPANY ? 'Acme, Inc.' : 'My workspace'}
              disabled={busy}
              className="mt-2 w-full rounded-xl border border-brand-border bg-brand-bg px-4 py-3 text-base text-brand-navy placeholder:text-brand-muted/60 focus:border-brand-blue focus:outline-none focus:ring-2 focus:ring-brand-blue/20 disabled:opacity-60 dark:border-brand-dark-border dark:bg-brand-dark-bg dark:text-brand-dark-navy dark:placeholder:text-brand-dark-muted"
            />
            <p className="mt-2 text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              {workspaceType === WORKSPACE_TYPE.COMPANY
                ? 'This is the name teammates will see in invitations and the dashboard.'
                : 'Leave this blank to use “My workspace.” Only you can see it until you invite someone.'}
            </p>
          </div>
        )}

        {displayedError && (
          <p id={errorId} role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-600 dark:bg-red-900/20 dark:text-red-400">
            {displayedError}
          </p>
        )}

        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-between">
          {onBack ? (
            <button
              type="button"
              onClick={onBack}
              disabled={busy}
              className="min-h-11 rounded-xl border border-brand-border px-5 py-3 text-sm font-medium text-brand-navy transition-colors hover:border-brand-border-mid disabled:opacity-50 dark:border-brand-dark-border dark:text-brand-dark-navy dark:hover:border-brand-dark-border-mid"
            >
              Back
            </button>
          ) : <span />}
          <button
            type="submit"
            disabled={!workspaceType || busy}
            className="min-h-11 rounded-xl bg-brand-blue px-6 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy
              ? 'Setting up…'
              : workspaceType === WORKSPACE_TYPE.COMPANY
                ? 'Create company workspace'
                : workspaceType === WORKSPACE_TYPE.PERSONAL
                  ? 'Create personal workspace'
                  : 'Choose a workspace'}
          </button>
        </div>
      </form>

      <aside className="mt-7 rounded-xl border border-brand-teal/30 bg-brand-teal-dim px-4 py-3 text-xs sm:text-sm leading-relaxed text-brand-teal dark:bg-brand-dark-teal-dim">
        <strong className="font-medium">Joining an existing company?</strong>{' '}
        Don’t create another workspace. Open the invitation link from your company admin and sign in with the exact email address they invited.
      </aside>
    </section>
  )
}
