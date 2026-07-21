import { useState } from 'react'
import {
  confirmationPathForLoginAudience,
  LOGIN_AUDIENCE,
  resendSignupConfirmation,
  supabase,
} from '../lib/supabase.js'
import { capture } from '../lib/analytics.js'

const AUDIENCE_CONTENT = {
  [LOGIN_AUDIENCE.PERSONAL]: {
    label: 'Personal workspace',
    title: 'Personal sign in',
    description: 'Open your individual projects, usage, API keys, and billing.',
    alternative: 'Need a company workspace?',
    alternativeLabel: 'Enterprise sign in',
    alternativeHref: '/login/enterprise',
  },
  [LOGIN_AUDIENCE.ENTERPRISE]: {
    label: 'Enterprise workspace',
    title: 'Enterprise sign in',
    description: 'Open your company workspace, members, roles, customers, service keys, and consolidated billing.',
    alternative: 'Signing in for yourself?',
    alternativeLabel: 'Personal sign in',
    alternativeHref: '/login/personal',
  },
}

export default function Auth({
  darkMode,
  onToggleDark,
  initialMode = 'login',
  loginAudience = '',
  onPasswordUpdated,
}) {
  const [mode, setMode]       = useState(initialMode)
  const [email, setEmail]     = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirmation, setPasswordConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')
  const [notice, setNotice]   = useState('')
  const [acceptedTerms, setAcceptedTerms] = useState(false)
  const [confirmationEmail, setConfirmationEmail] = useState('')
  const [resending, setResending] = useState(false)
  const audienceContent = AUDIENCE_CONTENT[loginAudience]
  const confirmationRedirect = `${window.location.origin}${confirmationPathForLoginAudience(loginAudience)}`

  const reset = () => { setError(''); setNotice('') }

  async function handleSubmit(e) {
    e.preventDefault()
    setLoading(true)
    reset()

    try {
      if (mode === 'login') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
        capture('login_completed', { login_audience: loginAudience || 'unspecified' })
      } else if (mode === 'signup') {
        capture('signup_started')
        const { error } = await supabase.auth.signUp({
          email,
          password,
          options: {
            emailRedirectTo: confirmationRedirect,
            data: {
              accepted_terms_at: new Date().toISOString(),
              terms_version: '2026-07-14',
              privacy_version: '2026-07-15',
              analytics_notice_acknowledged_at: new Date().toISOString(),
            },
          },
        })
        if (error) throw error
        capture('signup_submitted')
        setConfirmationEmail(email)
        setNotice('Request accepted. If this address needs confirmation, check your inbox or resend below. Already confirmed? Sign in or reset your password.')
        setMode('login')
      } else if (mode === 'reset') {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: `${window.location.origin}/dashboard`,
        })
        if (error) throw error
        capture('password_reset_requested')
        setNotice('Password reset link sent — check your email.')
        setMode('login')
      } else if (mode === 'recovery') {
        if (password !== passwordConfirmation) {
          throw new Error('Passwords do not match.')
        }
        const { error } = await supabase.auth.updateUser({ password })
        if (error) throw error
        capture('password_updated')
        onPasswordUpdated?.()
      }
    } catch (err) {
      if (mode === 'login' && err.message.toLowerCase().includes('email not confirmed')) {
        setConfirmationEmail(email)
      }
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function resendConfirmation() {
    setResending(true)
    setError('')
    try {
      await resendSignupConfirmation(
        confirmationEmail,
        confirmationRedirect,
      )
      setNotice('Confirmation request accepted. Check your inbox and its existing Brevitas email thread.')
    } catch (err) {
      setError(err.message)
    } finally {
      setResending(false)
    }
  }

  const isReset = mode === 'reset'
  const isRecovery = mode === 'recovery'

  if (mode === 'login' && !audienceContent && !notice && !confirmationEmail) {
    return (
      <LoginAudienceChoice darkMode={darkMode} onToggleDark={onToggleDark} />
    )
  }

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col items-center justify-center px-4 py-8 sm:py-12">
      {/* Dark mode toggle */}
      <button
        onClick={onToggleDark}
        className="fixed top-3 right-3 sm:top-5 sm:right-5 w-11 h-11 inline-flex items-center justify-center text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
        aria-label="Toggle dark mode"
      >
        {darkMode
          ? <SunIcon />
          : <MoonIcon />}
      </button>

      {/* Card */}
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-7 sm:mb-10 flex justify-center">
          <a href="/" className="no-underline" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-9 sm:h-10 w-auto dark:hidden" />
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-9 sm:h-10 w-auto hidden dark:block" />
          </a>
        </div>

        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl sm:rounded-2xl p-5 sm:p-8 shadow-sm">
          {audienceContent && (
            <p className="mb-3 text-[10px] font-medium uppercase tracking-[0.18em] text-brand-blue">
              {audienceContent.label}
            </p>
          )}
          <h1 className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy mb-1">
            {mode === 'login'  && (audienceContent?.title || 'Sign in')}
            {mode === 'signup' && 'Create account'}
            {mode === 'reset'  && 'Reset password'}
            {mode === 'recovery' && 'Choose a new password'}
          </h1>
          <p className="text-[12px] text-brand-muted dark:text-brand-dark-muted mb-6">
            {mode === 'login'  && (audienceContent?.description || 'Welcome back.')}
            {mode === 'signup' && 'Your dashboard is ready in seconds.'}
            {mode === 'reset'  && "We'll email you a reset link."}
            {mode === 'recovery' && 'Enter a new password for your account.'}
          </p>

          {notice && (
            <div className="mb-4 px-4 py-3 rounded-xl bg-brand-blue-dim dark:bg-brand-dark-blue-dim text-brand-blue text-[12px]">
              {notice}
            </div>
          )}
          {error && (
            <div className="mb-4 px-4 py-3 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 text-[12px]">
              {error}
            </div>
          )}
          {mode === 'login' && confirmationEmail && (
            <button
              type="button"
              onClick={resendConfirmation}
              disabled={resending}
              className="mb-4 text-[11px] text-brand-blue hover:underline disabled:opacity-50"
            >
              {resending ? 'Resending…' : 'Resend confirmation email'}
            </button>
          )}

          <form onSubmit={handleSubmit} className="space-y-3 ph-no-capture" data-ph-sensitive>
            {!isRecovery && <div>
              <label className="block text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted mb-1.5">
                Email
              </label>
              <input
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full px-3.5 py-2.5 rounded-xl border border-brand-border dark:border-brand-dark-border bg-brand-bg dark:bg-brand-dark-bg text-brand-navy dark:text-brand-dark-navy text-sm placeholder:text-brand-muted/40 dark:placeholder:text-brand-dark-muted/40 focus:outline-none focus:ring-2 focus:ring-brand-blue/20 transition"
              />
            </div>}

            {!isReset && (
              <div>
                <label className="block text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted mb-1.5">
                  Password
                </label>
                <input
                  type="password"
                  autoComplete={isRecovery ? 'new-password' : 'current-password'}
                  required
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="••••••••"
                  minLength={6}
                  className="w-full px-3.5 py-2.5 rounded-xl border border-brand-border dark:border-brand-dark-border bg-brand-bg dark:bg-brand-dark-bg text-brand-navy dark:text-brand-dark-navy text-sm placeholder:text-brand-muted/40 dark:placeholder:text-brand-dark-muted/40 focus:outline-none focus:ring-2 focus:ring-brand-blue/20 transition"
                />
              </div>
            )}

            {mode === 'signup' && (
              <label className="flex items-start gap-2.5 text-[11px] leading-relaxed text-brand-muted dark:text-brand-dark-muted py-1">
                <input
                  type="checkbox"
                  required
                  checked={acceptedTerms}
                  onChange={e => setAcceptedTerms(e.target.checked)}
                  className="mt-0.5 shrink-0 accent-brand-blue"
                />
                <span>
                  I agree to the <a href="/terms" target="_blank" rel="noreferrer" className="text-brand-blue underline">Terms of Service</a>, including its arbitration and class-action waiver, and acknowledge the <a href="/privacy" target="_blank" rel="noreferrer" className="text-brand-blue underline">Privacy Policy</a>, including automatic analytics and strictly masked session replay with an anytime opt-out.
                </span>
              </label>
            )}

            {isRecovery && (
              <div>
                <label className="block text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted mb-1.5">
                  Confirm new password
                </label>
                <input
                  type="password"
                  required
                  autoComplete="new-password"
                  value={passwordConfirmation}
                  onChange={e => setPasswordConfirmation(e.target.value)}
                  placeholder="••••••••"
                  minLength={6}
                  className="w-full px-3.5 py-2.5 rounded-xl border border-brand-border dark:border-brand-dark-border bg-brand-bg dark:bg-brand-dark-bg text-brand-navy dark:text-brand-dark-navy text-sm placeholder:text-brand-muted/40 dark:placeholder:text-brand-dark-muted/40 focus:outline-none focus:ring-2 focus:ring-brand-blue/20 transition"
                />
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 rounded-xl bg-brand-blue text-white text-[11px] tracking-widest uppercase font-medium hover:opacity-90 transition disabled:opacity-50 mt-1"
            >
              {loading
                ? 'Please wait…'
                : mode === 'login'  ? `Sign in${audienceContent ? ` to ${loginAudience}` : ''}`
                : mode === 'signup' ? 'Create account'
                : mode === 'recovery' ? 'Update password'
                : 'Send reset link'}
            </button>
          </form>

          {/* Footer links */}
          {!isRecovery && <div className="mt-5 flex flex-col gap-2 items-center">
            {mode === 'login' && (
              <>
                <button
                  onClick={() => { setMode('signup'); setConfirmationEmail(''); reset() }}
                  className="text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
                >
                  No account? Sign up
                </button>
                <button
                  onClick={() => { setMode('reset'); reset() }}
                  className="text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
                >
                  Forgot password?
                </button>
                {audienceContent && (
                  <p className="pt-2 text-center text-[11px] text-brand-muted dark:text-brand-dark-muted">
                    {audienceContent.alternative}{' '}
                    <a href={audienceContent.alternativeHref} className="text-brand-blue hover:underline">
                      {audienceContent.alternativeLabel}
                    </a>
                  </p>
                )}
              </>
            )}
            {(mode === 'signup' || mode === 'reset') && (
              <button
                onClick={() => { setMode('login'); reset() }}
                className="text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
              >
                Back to sign in
              </button>
            )}
          </div>}
        </div>
        <div className="mt-5 flex justify-center gap-4 text-[11px] text-brand-muted dark:text-brand-dark-muted">
          <a href="/privacy" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Privacy</a>
          <a href="/terms" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Terms</a>
        </div>
      </div>
    </div>
  )
}

function LoginAudienceChoice({ darkMode, onToggleDark }) {
  const choices = [
    {
      audience: LOGIN_AUDIENCE.PERSONAL,
      eyebrow: 'For you',
      title: 'Personal',
      description: 'Individual projects, usage, API keys, and billing.',
      href: '/login/personal',
      action: 'Continue as a user',
    },
    {
      audience: LOGIN_AUDIENCE.ENTERPRISE,
      eyebrow: 'For your organization',
      title: 'Enterprise',
      description: 'Company members, roles, customers, service keys, and consolidated billing.',
      href: '/login/enterprise',
      action: 'Continue as an enterprise',
    },
  ]

  return (
    <div className="min-h-screen bg-brand-bg px-4 py-8 dark:bg-brand-dark-bg sm:py-12">
      <button
        onClick={onToggleDark}
        className="fixed right-3 top-3 inline-flex h-11 w-11 items-center justify-center text-brand-muted transition-colors hover:text-brand-navy dark:text-brand-dark-muted dark:hover:text-brand-dark-navy sm:right-5 sm:top-5"
        aria-label="Toggle dark mode"
      >
        {darkMode ? <SunIcon /> : <MoonIcon />}
      </button>

      <main className="mx-auto flex min-h-[calc(100vh-4rem)] w-full max-w-4xl flex-col justify-center sm:min-h-[calc(100vh-6rem)]">
        <div className="mb-8 flex justify-center sm:mb-10">
          <a href="/" className="no-underline" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-9 w-auto dark:hidden sm:h-10" />
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="hidden h-9 w-auto dark:block sm:h-10" />
          </a>
        </div>

        <section aria-labelledby="login-workspace-title">
          <header className="mx-auto mb-7 max-w-2xl text-center sm:mb-10">
            <p className="annotation mb-3 uppercase tracking-widest">Choose your sign-in</p>
            <h1 id="login-workspace-title" className="font-serif text-4xl leading-tight text-brand-navy dark:text-brand-dark-navy sm:text-5xl">
              Which workspace are you opening?
            </h1>
            <p className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted sm:text-base">
              Use one Brevitas account for both. Your verified membership determines which workspaces you can open after sign-in.
            </p>
          </header>

          <div className="grid gap-4 md:grid-cols-2">
            {choices.map(choice => (
              <a
                key={choice.audience}
                href={choice.href}
                className="group flex min-h-64 flex-col rounded-2xl border border-brand-border bg-white p-6 no-underline shadow-sm transition hover:-translate-y-0.5 hover:border-brand-blue/40 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-brand-blue/30 dark:border-brand-dark-border dark:bg-brand-dark-surface dark:hover:border-brand-blue/60 sm:p-8"
              >
                <span className="text-[10px] font-medium uppercase tracking-[0.18em] text-brand-blue">{choice.eyebrow}</span>
                <span className="mt-5 font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">{choice.title}</span>
                <span className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">{choice.description}</span>
                <span className="mt-auto pt-8 text-sm font-medium text-brand-blue">
                  {choice.action} <span aria-hidden="true" className="transition-transform group-hover:translate-x-1">→</span>
                </span>
              </a>
            ))}
          </div>
        </section>

        <div className="mt-7 flex justify-center gap-4 text-[11px] text-brand-muted dark:text-brand-dark-muted">
          <a href="/privacy" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Privacy</a>
          <a href="/terms" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Terms</a>
        </div>
      </main>
    </div>
  )
}

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  )
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5"/>
      <line x1="12" y1="1" x2="12" y2="3"/>
      <line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/>
      <line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  )
}
