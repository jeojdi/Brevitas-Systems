import { useState } from 'react'
import { supabase } from '../lib/supabase.js'

export default function Auth({ darkMode, onToggleDark, initialMode = 'login', onPasswordUpdated }) {
  const [mode, setMode]       = useState(initialMode)
  const [email, setEmail]     = useState('')
  const [password, setPassword] = useState('')
  const [passwordConfirmation, setPasswordConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')
  const [notice, setNotice]   = useState('')

  const reset = () => { setError(''); setNotice('') }

  async function handleSubmit(e) {
    e.preventDefault()
    setLoading(true)
    reset()

    try {
      if (mode === 'login') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
      } else if (mode === 'signup') {
        const { error } = await supabase.auth.signUp({
          email,
          password,
          options: { emailRedirectTo: `${window.location.origin}/email-confirmed` },
        })
        if (error) throw error
        setNotice('Check your email to confirm your account, then sign in.')
        setMode('login')
      } else if (mode === 'reset') {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: `${window.location.origin}/dashboard`,
        })
        if (error) throw error
        setNotice('Password reset link sent — check your email.')
        setMode('login')
      } else if (mode === 'recovery') {
        if (password !== passwordConfirmation) {
          throw new Error('Passwords do not match.')
        }
        const { error } = await supabase.auth.updateUser({ password })
        if (error) throw error
        onPasswordUpdated?.()
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const isReset = mode === 'reset'
  const isRecovery = mode === 'recovery'

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col items-center justify-center px-4">
      {/* Dark mode toggle */}
      <button
        onClick={onToggleDark}
        className="fixed top-5 right-5 text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
        aria-label="Toggle dark mode"
      >
        {darkMode
          ? <SunIcon />
          : <MoonIcon />}
      </button>

      {/* Card */}
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-10 flex justify-center">
          <a href="/" className="no-underline" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-10 w-auto dark:hidden" />
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-10 w-auto hidden dark:block" />
          </a>
        </div>

        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-8 shadow-sm">
          <h1 className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy mb-1">
            {mode === 'login'  && 'Sign in'}
            {mode === 'signup' && 'Create account'}
            {mode === 'reset'  && 'Reset password'}
            {mode === 'recovery' && 'Choose a new password'}
          </h1>
          <p className="text-[12px] text-brand-muted dark:text-brand-dark-muted mb-6">
            {mode === 'login'  && 'Welcome back.'}
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

          <form onSubmit={handleSubmit} className="space-y-3">
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
                : mode === 'login'  ? 'Sign in'
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
                  onClick={() => { setMode('signup'); reset() }}
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
      </div>
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
