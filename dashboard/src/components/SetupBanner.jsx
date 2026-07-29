import { useCallback, useEffect, useRef, useState } from 'react'

/** How often the bar re-checks on its own while setup is still incomplete. */
export const SETUP_POLL_MS = 15_000

/**
 * The remaining setup work, moved out of the way.
 *
 * Installing BVX and proving a real proxied request used to be a wall in front of
 * the dashboard. They are the same two facts, checked the same way, but now they
 * sit in a bar the user can ignore: the button opens the Connect tab, and when
 * both facts land the bar marks setup finished and gets out of the way.
 *
 * @param {{
 *   onCheck: () => Promise<{cliConnected: boolean, proxiedRequestObserved: boolean}>,
 *   onComplete: () => Promise<unknown>,
 *   onOpenSetup: () => void,
 * }} props
 */
export default function SetupBanner({ onCheck, onComplete, onOpenSetup }) {
  const [status, setStatus] = useState({ cliConnected: false, proxiedRequestObserved: false })
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState('')
  const [checkedAt, setCheckedAt] = useState('')
  const inFlight = useRef(false)
  const completing = useRef(false)

  const check = useCallback(async ({ manual = false } = {}) => {
    if (!onCheck || inFlight.current || completing.current) return
    inFlight.current = true
    setChecking(true)
    if (manual) setError('')
    try {
      const next = await onCheck()
      setStatus({
        cliConnected: next.cliConnected,
        proxiedRequestObserved: next.proxiedRequestObserved,
      })
      setError('')
      setCheckedAt(new Date().toLocaleTimeString())
      if (next.cliConnected && next.proxiedRequestObserved) {
        // Both facts are in. Recording completion is what removes this bar; a
        // failure here is reported rather than left as a silent retry loop.
        completing.current = true
        await onComplete?.()
      }
    } catch (reason) {
      completing.current = false
      setError(reason instanceof Error ? reason.message : 'Setup status is temporarily unavailable.')
    } finally {
      inFlight.current = false
      setChecking(false)
    }
  }, [onCheck, onComplete])

  useEffect(() => {
    if (!onCheck) return undefined
    check()
    const timer = window.setInterval(check, SETUP_POLL_MS)
    return () => window.clearInterval(timer)
  }, [check, onCheck])

  return (
    <div
      data-testid="setup-banner"
      className="mb-5 rounded-xl border border-brand-blue/30 bg-brand-blue-dim px-4 py-3 dark:bg-brand-dark-blue-dim"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-medium text-brand-navy dark:text-brand-dark-navy">
            Finish setup
          </p>
          <p className="mt-0.5 text-[11px] leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            Install BVX and send one prompt through it to start tracking. Everything here
            works meanwhile — this bar disappears on its own once both land.
          </p>
          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]" aria-live="polite">
            <li className={status.cliConnected ? 'text-brand-teal' : 'text-brand-muted dark:text-brand-dark-muted'}>
              <span aria-hidden="true">{status.cliConnected ? '✓' : '○'}</span> CLI connected
            </li>
            <li className={status.proxiedRequestObserved ? 'text-brand-teal' : 'text-brand-muted dark:text-brand-dark-muted'}>
              <span aria-hidden="true">{status.proxiedRequestObserved ? '✓' : '○'}</span> First request observed
            </li>
          </ul>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => check({ manual: true })}
            disabled={checking}
            className="min-h-10 rounded-xl border border-brand-blue/40 px-3 py-2 text-[11px] font-medium uppercase tracking-widest text-brand-blue transition disabled:opacity-50"
          >
            {checking ? 'Checking…' : 'Check now'}
          </button>
          <button
            type="button"
            onClick={onOpenSetup}
            className="min-h-10 rounded-xl bg-brand-blue px-4 py-2 text-[11px] font-medium uppercase tracking-widest text-white transition hover:opacity-90"
          >
            Set up
          </button>
        </div>
      </div>

      {error
        ? <p role="alert" className="mt-2 text-[11px] text-red-600 dark:text-red-400">{error}</p>
        : checkedAt && <p className="mt-2 text-[11px] text-brand-muted dark:text-brand-dark-muted">Last checked {checkedAt}.</p>}
    </div>
  )
}
