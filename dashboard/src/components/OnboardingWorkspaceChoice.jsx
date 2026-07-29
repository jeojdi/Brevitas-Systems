import { useId, useState } from 'react'
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

/**
 * The one thing that must happen before the dashboard can open: a workspace.
 *
 * Installing BVX and proving a proxied request used to be steps 2 and 3 of a
 * blocking wizard, which meant a new account could not see the product until a
 * CLI install and a real request had both landed. Those now live in the
 * dashboard's own Connect tab, surfaced by a setup bar, so this screen ends the
 * moment the workspace exists.
 */
export default function OnboardingWorkspaceChoice({
  initialWorkspaceType = '',
  initialWorkspaceName = '',
  isSubmitting = false,
  errorMessage = '',
  onContinue,
  onBack,
  onWorkspaceTypeChange,
  onWorkspaceNameChange,
}) {
  const headingId = useId()
  const helpId = useId()
  const errorId = useId()
  const fieldName = useId()
  const [workspaceType, setWorkspaceType] = useState(initialWorkspaceType)
  const [workspaceName, setWorkspaceName] = useState(initialWorkspaceName)
  const [showAllChoices, setShowAllChoices] = useState(!initialWorkspaceType)
  const [validationError, setValidationError] = useState('')
  const [localSubmitting, setLocalSubmitting] = useState(false)
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
      // The caller opens the dashboard once the workspace exists. There is no
      // next step to advance to here.
      await onContinue?.(selection)
    } catch (reason) {
      setValidationError(reason instanceof Error ? reason.message : 'Workspace setup could not continue.')
    } finally {
      setLocalSubmitting(false)
    }
  }

  const isCompany = workspaceType === WORKSPACE_TYPE.COMPANY
  const visibleChoices = showAllChoices
    ? choices
    : choices.filter(choice => choice.value === workspaceType)

  return (
    <section aria-labelledby={headingId} className="w-full max-w-5xl mx-auto">
      <header className="max-w-2xl mx-auto text-center mb-7 sm:mb-10">
        <p className="annotation tracking-widest uppercase mb-3">{isCompany ? 'Enterprise setup' : workspaceType ? 'Personal setup' : 'Choose your path'}</p>
        <h1 id={headingId} className="font-serif text-4xl sm:text-5xl leading-tight text-brand-navy dark:text-brand-dark-navy">
          {isCompany ? 'Set up your enterprise boundary.' : workspaceType ? 'Set up your personal workspace.' : 'How will you use Brevitas?'}
        </h1>
        <p id={helpId} className="mt-3 text-sm sm:text-base leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid">
          {isCompany
            ? 'Your company gets shared repositories, role-based access, scoped service keys, consolidated usage, and billing.'
            : workspaceType
              ? 'Your dashboard opens as soon as this exists. Connecting a tool is waiting for you inside it.'
              : 'Choose the lighter personal experience or a company boundary with roles and production credentials.'}
        </p>
      </header>

      <form onSubmit={submit} aria-busy={busy} aria-describedby={`${helpId}${displayedError ? ` ${errorId}` : ''}`}>
        <fieldset disabled={busy}>
          <legend className="sr-only">Choose a workspace type</legend>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 sm:gap-5">
            {visibleChoices.map(choice => {
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

        {!showAllChoices && (
          <button
            type="button"
            onClick={() => setShowAllChoices(true)}
            disabled={busy}
            className="mx-auto mt-4 block min-h-11 px-4 py-2 text-xs font-medium text-brand-blue hover:underline disabled:opacity-50"
          >
            Compare personal and enterprise setup
          </button>
        )}

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
