import { useState } from 'react'
import { authErrorMessage, supabase } from '../lib/supabase.js'
import {
  VERIFICATION_SENT_NOTICE,
  emailVerificationRedirect,
  sendEmailVerificationLink,
} from '../lib/email-verification.js'
import { capture } from '../lib/analytics.js'

/**
 * Offer a signed-in user the chance to prove their email address.
 *
 * Signup no longer waits on a confirmation link, so this is where ownership gets
 * established. Nothing in the dashboard is gated on it: the bar is an errand, not
 * a wall, and it disappears once the emailed link has been followed.
 *
 * @param {{ email: string }} props
 */
export default function EmailVerificationBanner({ email }) {
  const [sent, setSent]     = useState(false)
  const [busy, setBusy]     = useState(false)
  const [error, setError]   = useState('')

  async function handleSend() {
    setBusy(true)
    setError('')
    try {
      await sendEmailVerificationLink(email, emailVerificationRedirect(window.location), supabase.auth)
      capture('email_verification_link_sent')
      setSent(true)
    } catch (err) {
      setError(authErrorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      data-testid="email-verification-banner"
      className="mb-5 rounded-xl border border-brand-blue/30 bg-brand-blue-dim px-4 py-3 dark:bg-brand-dark-blue-dim"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-medium text-brand-navy dark:text-brand-dark-navy">
            {sent ? 'Verification link sent' : 'Verify your email'}
          </p>
          <p data-ph-sensitive className="ph-no-capture mt-0.5 text-[11px] leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            {sent
              ? VERIFICATION_SENT_NOTICE
              : `Confirm ${email} so we can reach you about billing and security. Your dashboard works either way.`}
          </p>
        </div>

        <button
          type="button"
          onClick={handleSend}
          disabled={busy}
          className={`shrink-0 min-h-10 rounded-xl px-4 py-2 text-[11px] font-medium uppercase tracking-widest transition disabled:opacity-50 ${
            sent
              ? 'border border-brand-blue/40 text-brand-blue hover:bg-white/50 dark:hover:bg-brand-dark-surface/50'
              : 'bg-brand-blue text-white hover:opacity-90'
          }`}
        >
          {busy ? 'Sending…' : sent ? 'Resend link' : 'Verify email'}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-2 text-[11px] text-red-600 dark:text-red-400">{error}</p>
      )}
    </div>
  )
}
