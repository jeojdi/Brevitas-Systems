'use client'

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  acceptCompanyInvitation,
  consumeCompanyInvitationFragment,
  isCompanyInvitationToken,
} from '../lib/company-invitation.js'

// Capture and scrub before React mounts. This runs while the module graph is
// evaluated and keeps the secret in this tab's memory only (never web storage).
let pendingFragmentInvitation = typeof window === 'undefined'
  ? { found: false, token: '' }
  : consumeCompanyInvitationFragment()

const roleLabel = role => String(role || '').replaceAll('_', ' ')

export function clearPendingCompanyInvitation() {
  pendingFragmentInvitation = { found: false, token: '' }
}

export function hasPendingCompanyInvitation() {
  return pendingFragmentInvitation.found
}

export default function InvitationAcceptance({
  invitationToken,
  accessToken = '',
  onAccepted,
  onAuthenticationRequired,
  onDismiss,
  onError,
  onUseDifferentAccount,
  request = fetch,
  requestId,
}) {
  const tokenRef = useRef('')
  const requestRef = useRef(null)
  const attemptRef = useRef(0)
  const [phase, setPhase] = useState('checking')
  const [error, setError] = useState('')
  const [acceptedRole, setAcceptedRole] = useState('')

  useLayoutEffect(() => {
    attemptRef.current += 1
    requestRef.current?.abort()
    requestRef.current = null
    setError('')
    setAcceptedRole('')

    const hasExplicitToken = invitationToken !== undefined && invitationToken !== null
    const candidate = hasExplicitToken ? invitationToken : pendingFragmentInvitation.token
    const found = hasExplicitToken ? String(invitationToken).length > 0 : pendingFragmentInvitation.found
    if (!found) {
      tokenRef.current = ''
      setPhase('absent')
    } else if (!isCompanyInvitationToken(candidate)) {
      tokenRef.current = ''
      clearPendingCompanyInvitation()
      setPhase('invalid')
    } else {
      tokenRef.current = candidate
      setPhase('ready')
    }
  }, [invitationToken])

  useEffect(() => () => {
    attemptRef.current += 1
    requestRef.current?.abort()
    tokenRef.current = ''
  }, [])

  const dismiss = () => {
    attemptRef.current += 1
    requestRef.current?.abort()
    requestRef.current = null
    tokenRef.current = ''
    clearPendingCompanyInvitation()
    setPhase('dismissed')
    onDismiss?.()
  }

  const accept = async () => {
    if (phase !== 'ready') return
    if (!accessToken) {
      onAuthenticationRequired?.()
      return
    }
    const token = tokenRef.current
    const controller = new AbortController()
    const attempt = attemptRef.current + 1
    attemptRef.current = attempt
    requestRef.current?.abort()
    requestRef.current = controller
    setError('')
    setPhase('accepting')
    try {
      const result = await acceptCompanyInvitation(accessToken, token, {
        request,
        requestId,
        signal: controller.signal,
      })
      if (attemptRef.current !== attempt) return
      tokenRef.current = ''
      clearPendingCompanyInvitation()
      setAcceptedRole(result.role)
      setPhase('accepted')
      // The membership is already committed. A consumer callback must not turn
      // that success into a retryable invitation state.
      try { onAccepted?.(result) } catch { /* integration callback failed */ }
    } catch (reason) {
      if (attemptRef.current !== attempt || reason?.name === 'AbortError') return
      setError(reason?.message || 'Invitation acceptance is temporarily unavailable. Try again.')
      setPhase('ready')
      onError?.(reason)
    } finally {
      if (requestRef.current === controller) requestRef.current = null
    }
  }

  if (phase === 'checking' || phase === 'absent' || phase === 'dismissed') return null

  if (phase === 'invalid') {
    return <section className="min-h-screen flex items-center justify-center px-4 py-10 ph-no-capture" data-ph-sensitive aria-labelledby="invitation-title">
      <div className="w-full max-w-lg rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-6 sm:p-8 shadow-sm">
        <p className="annotation tracking-widest uppercase">Company invitation</p>
        <h1 id="invitation-title" className="mt-2 font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">This invitation link is incomplete.</h1>
        <p role="alert" className="mt-3 text-sm text-brand-muted dark:text-brand-dark-muted">Ask your company administrator for a new invitation link. Invitation links expire and can be used only once.</p>
        <button type="button" onClick={dismiss} className="mt-6 min-h-11 rounded-xl border border-brand-border dark:border-brand-dark-border px-4 text-xs text-brand-navy dark:text-brand-dark-navy">Return to dashboard</button>
      </div>
    </section>
  }

  if (phase === 'accepted') {
    return <section className="min-h-screen flex items-center justify-center px-4 py-10 ph-no-capture" data-ph-sensitive aria-labelledby="invitation-title" aria-live="polite">
      <div className="w-full max-w-lg rounded-2xl border border-brand-teal/40 bg-white dark:bg-brand-dark-surface p-6 sm:p-8 shadow-sm">
        <p className="annotation tracking-widest uppercase text-brand-teal">Invitation accepted</p>
        <h1 id="invitation-title" className="mt-2 font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">You joined your company workspace.</h1>
        <p className="mt-3 text-sm text-brand-muted dark:text-brand-dark-muted">Your role is {roleLabel(acceptedRole)}. Continue to the dashboard to start working with your team.</p>
        <button type="button" onClick={dismiss} className="mt-6 min-h-11 rounded-xl bg-brand-blue px-5 text-xs font-medium uppercase tracking-widest text-white">Continue to dashboard</button>
      </div>
    </section>
  }

  const accepting = phase === 'accepting'
  const authenticated = Boolean(accessToken)
  return <section className="min-h-screen flex items-center justify-center px-4 py-10 ph-no-capture" data-ph-sensitive aria-labelledby="invitation-title">
    <div className="w-full max-w-lg rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-6 sm:p-8 shadow-sm">
      <p className="annotation tracking-widest uppercase">Private company invitation</p>
      <h1 id="invitation-title" className="mt-2 font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">Join your team on Brevitas.</h1>
      {authenticated
        ? <p className="mt-3 text-sm text-brand-muted dark:text-brand-dark-muted">Confirm that you are signed in with the same verified email address that received the invitation. Brevitas will add this workspace without changing your existing workspaces.</p>
        : <div className="mt-3 space-y-2 text-sm text-brand-muted dark:text-brand-dark-muted">
          <p>Sign in or create an account with the exact email address that received this invitation.</p>
          <p className="text-xs">The private invitation is kept only in this tab. If email confirmation reloads the page, reopen the original invitation email after signing in.</p>
        </div>}

      {error && <p role="alert" className="mt-5 rounded-xl bg-red-50 dark:bg-red-900/20 px-4 py-3 text-xs text-red-600 dark:text-red-400">{error}</p>}

      <div className="mt-6 flex flex-col gap-3">
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button type="button" onClick={dismiss} disabled={accepting} className="min-h-11 rounded-xl border border-brand-border dark:border-brand-dark-border px-4 text-xs text-brand-muted disabled:opacity-50">Not now</button>
          <button
            type="button"
            onClick={authenticated ? accept : onAuthenticationRequired}
            disabled={accepting}
            className="min-h-11 rounded-xl bg-brand-blue px-5 text-xs font-medium uppercase tracking-widest text-white disabled:opacity-50"
          >
            {accepting ? 'Joining company…' : authenticated ? 'Accept invitation' : 'Sign in to continue'}
          </button>
        </div>
        {authenticated && onUseDifferentAccount && <button
          type="button"
          onClick={onUseDifferentAccount}
          disabled={accepting}
          className="min-h-11 self-center px-3 text-xs text-brand-muted underline-offset-4 hover:underline disabled:opacity-50"
        >Use a different account</button>}
      </div>
    </div>
  </section>
}
