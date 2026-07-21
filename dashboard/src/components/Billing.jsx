import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats, fetchBillingStatus, openBillingPortal, startBillingCheckout } from '../lib/api.js'

function fmt(n, decimals = 2) {
  return Number(n || 0).toFixed(decimals)
}
function fmtK(n) {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
}
function fmtDate(value) {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toISOString().slice(0, 10)
}

function StatCard({ label, value, sub, accent = false }) {
  return (
    <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6">
      <p className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted mb-3">{label}</p>
      <p className={`font-serif text-3xl ${accent ? 'text-brand-teal' : 'text-brand-navy dark:text-brand-dark-navy'} leading-none mb-1`}>{value}</p>
      {sub && <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted mt-2">{sub}</p>}
    </div>
  )
}

export default function Billing({ apiKey, accessToken, refreshTick, previewStats, previewBilling }) {
  const [stats, setStats]   = useState(previewStats || null)
  const [billing, setBilling] = useState(previewBilling || null)
  const [loading, setLoading] = useState(!previewStats)
  const [error, setError]   = useState('')
  const [billingError, setBillingError] = useState('')
  const [billingAction, setBillingAction] = useState('')
  const controllerRef = useRef(null)

  const load = useCallback(async () => {
    if (previewStats) { setStats(previewStats); setLoading(false); return }
    if (!apiKey) return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setError('')
    try {
      const data = await fetchStats(apiKey, { signal: controller.signal })
      if (controllerRef.current === controller) setStats(data)
    } catch (e) {
      if (controllerRef.current === controller && e.name !== 'AbortError') setError(e.message)
    } finally {
      if (controllerRef.current === controller) setLoading(false)
    }
  }, [apiKey, previewStats])

  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load, refreshTick])

  const loadBilling = useCallback(async () => {
    if (previewBilling) { setBilling(previewBilling); return }
    if (!accessToken) return
    try {
      setBilling(await fetchBillingStatus(accessToken))
    } catch (e) {
      setBillingError(e.message)
    }
  }, [accessToken, previewBilling])

  useEffect(() => { loadBilling() }, [loadBilling, refreshTick])

  const goToStripe = async kind => {
    setBillingAction(kind)
    setBillingError('')
    try {
      const action = kind === 'checkout' ? startBillingCheckout : openBillingPortal
      const { url } = await action(accessToken)
      window.location.assign(url)
    } catch (e) {
      if (e.status === 409 && kind === 'checkout') {
        try {
          const { url } = await openBillingPortal(accessToken)
          window.location.assign(url)
          return
        } catch (portalError) {
          setBillingError(portalError.message)
        }
      } else {
        setBillingError(e.message)
      }
    } finally {
      setBillingAction('')
    }
  }

  if (!apiKey) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">// no active API key</p>
    </div>
  )

  if (loading) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">Loading savings data…</p>
    </div>
  )

  if (error && !stats) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-red-500">{error}</p>
      <button onClick={load} className="annotation mt-3 hover:text-brand-blue">retry</button>
    </div>
  )

  const verifiedSaved  = Number(stats?.total_verified_savings_usd || 0)
  const providerSpend  = Number(stats?.total_actual_cost_usd || 0)
  const weeks          = stats?.billing_by_week || []
  const thisWeek       = weeks[0] || null
  const allUnpriced    = Number(stats?.total_calls || 0) > 0 && Number(stats?.unpriced_calls || 0) === Number(stats?.total_calls || 0)
  const billingActive  = ['active', 'trialing'].includes(billing?.subscription_status)
  const billingManageable = ['active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'].includes(billing?.subscription_status)

  return (
    <div className="space-y-10" data-ph-sensitive>
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Savings</p>
        <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          Track what Brevitas saves.
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-3 max-w-lg leading-relaxed">
          Provider-receipt costs and quality-safe net savings. Unpriced calls remain visible without a guessed dollar value.
        </p>
      </div>

      {/* Stripe-hosted usage billing */}
      <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6 sm:p-8">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
          <div className="max-w-2xl">
            <div className="flex flex-wrap items-center gap-3 mb-3">
              <p className="annotation tracking-widest uppercase">// secure billing</p>
              {billing && (
                <span className={`font-mono text-[10px] px-2 py-1 rounded-full ${billingActive ? 'bg-emerald-50 dark:bg-emerald-950/30 text-emerald-600' : 'bg-brand-bg dark:bg-brand-dark-bg text-brand-muted'}`}>
                  {billingActive ? 'active' : billing.subscription_status.replaceAll('_', ' ')}
                </span>
              )}
            </div>
            <h3 className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">25% of verified savings. Nothing else.</h3>
            <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-2 leading-relaxed">
              Stripe hosts card collection and the billing portal; Brevitas never receives card details. Usage is floored to micro-dollars, deduplicated{billing?.weekly_safety_cap_usd ? `, and constrained by a $${fmt(billing.weekly_safety_cap_usd, 0)} weekly safety cap` : ''}. Stripe closes and bills each metered period every seven days.
            </p>
            {billing && (
              <div className="flex flex-wrap gap-x-8 gap-y-2 mt-4 font-mono text-[11px] text-brand-muted dark:text-brand-dark-muted">
                <span>Current estimate <strong className="text-brand-navy dark:text-brand-dark-navy">{billing.period_tracking_valid ? `$${fmt(billing.estimated_fee_usd, 6)}` : 'Unavailable'}</strong></span>
                <span>Reported to Stripe <strong className="text-brand-navy dark:text-brand-dark-navy">{billing.period_tracking_valid ? `$${fmt(billing.reported_fee_usd, 6)}` : 'Unavailable'}</strong></span>
                {billing.period_tracking_valid && <span>Billing week <strong className="text-brand-navy dark:text-brand-dark-navy">{fmtDate(billing.current_period_start)} → {fmtDate(billing.current_period_end)}</strong></span>}
              </div>
            )}
            {billingError && <p className="font-mono text-xs text-red-500 mt-4">{billingError}</p>}
            {billingActive && billing && !billing.period_tracking_valid && <p className="font-mono text-xs text-red-500 mt-4">Weekly billing totals are unavailable because Stripe period boundaries have not synchronized. Charging is fail-closed for these entries.</p>}
            {billing?.needs_review > 0 && <p className="font-mono text-xs text-amber-600 mt-4">A billing event is paused for manual review and will not be retried automatically.</p>}
          </div>
          <button
            type="button"
            disabled={Boolean(previewBilling) || !billing?.configured || Boolean(billingAction)}
            onClick={() => goToStripe(billingManageable ? 'portal' : 'checkout')}
            className="shrink-0 min-h-11 px-5 py-3 rounded-xl bg-brand-blue text-white font-mono text-[11px] tracking-widest uppercase disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-90 transition-opacity"
          >
            {billingAction ? 'Opening Stripe…' : billingManageable ? 'Manage billing' : 'Set up billing'}
          </button>
        </div>
        {billing && !billing.configured && (
          <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted mt-4">Billing enrollment is not enabled in this environment.</p>
        )}
      </div>

      {/* All-time stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Total calls"
          value={fmtK(stats?.total_calls)}
          sub="recorded usage"
        />
        <StatCard
          label="Total tokens saved"
          value={fmtK(stats?.total_tokens_saved)}
          sub="across all calls"
        />
        <StatCard
          label="Provider spend"
          value={allUnpriced ? 'Unpriced' : `$${fmt(providerSpend, 4)}`}
          sub="from provider receipts"
        />
        <StatCard
          label="Verified savings"
          value={`$${fmt(verifiedSaved, 4)}`}
          sub="quality-safe methods only"
          accent
        />
      </div>

      {/* This week */}
      {thisWeek && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// week of {thisWeek.week_start}</p>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Calls"         value={fmtK(thisWeek.calls)} />
            <StatCard label="Tokens saved"  value={fmtK(thisWeek.tokens_saved)} />
            <StatCard label="Provider spend" value={`$${fmt(thisWeek.actual_cost_usd, 4)}`} />
            <StatCard label="Verified savings" value={`$${fmt(thisWeek.verified_savings_usd ?? thisWeek.cost_saved_usd, 4)}`} accent />
          </div>
        </div>
      )}

      {/* Weekly history table */}
      {weeks.length > 1 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// weekly usage history</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-x-auto">
            <div className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Week of', 'Calls', 'Tokens saved', 'Provider spend', 'Verified savings'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {weeks.map(m => (
              <div key={m.week_start} className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">{m.week_start}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.tokens_saved)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">${fmt(m.actual_cost_usd, 4)}</span>
                <span className="font-mono text-xs text-brand-teal">${fmt(m.verified_savings_usd ?? m.cost_saved_usd, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {weeks.length === 0 && (
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-10 sm:p-16 text-center">
          <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-2">No usage yet.</p>
          <p className="annotation">// start compressing to see usage and savings here</p>
        </div>
      )}

      {/* How savings are measured */}
      <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6 space-y-3">
        <p className="annotation tracking-widest uppercase mb-2">// how it works</p>
        <div className="space-y-2">
          {[
            ['Baseline tokens', 'What you would have sent to Anthropic/OpenAI without Brevitas'],
            ['Compressed tokens', 'What Brevitas actually sent after compression'],
            ['Tokens saved', 'The difference — real input tokens that never reached the provider'],
            ['Provider spend', 'What provider receipts say the optimized calls actually cost'],
            ['Verified savings', 'Positive savings from byte-preserving or workload-verified methods'],
          ].map(([term, def]) => (
            <div key={term} className="flex flex-col sm:flex-row gap-1 sm:gap-4">
              <span className="font-mono text-[11px] text-brand-blue shrink-0 sm:w-36">{term}</span>
              <span className="font-mono text-[11px] text-brand-muted dark:text-brand-dark-muted">{def}</span>
            </div>
          ))}
        </div>
        <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted pt-2">
          Questions? <a href="mailto:info@brevitassystems.com" className="text-brand-blue hover:underline">info@brevitassystems.com</a>
        </p>
      </div>
    </div>
  )
}
