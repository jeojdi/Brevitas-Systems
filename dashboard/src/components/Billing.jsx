import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats, fetchCacheStats, fetchBillingStatus, openBillingPortal, startBillingCheckout } from '../lib/api.js'
import { WITHHELD, spendRedacted } from '../lib/spend.js'

function fmt(n, decimals = 2) {
  return Number(n || 0).toFixed(decimals)
}
// Money that the API declined to state must never render as a number. `fmt`
// coerces null/undefined to 0, so `fmt(null, 6)` is the string "0.000000" -- a
// confident wrong $0 on a billing page, which is exactly the failure the
// settlement repoint (docs/STRIPE_FIX_PLAN.md FIX-5) exists to prevent. The API
// now returns null, never 0, whenever the Stripe period boundaries are not
// exactly one week or the settlement summary fails closed (no_billing_account,
// period_anchor_mismatch, guard_unavailable), and it sets settlement_pending
// while the money of record has no writer.
function fmtMoney(billing, value, decimals = 6) {
  if (!billing?.period_tracking_valid || billing?.settlement_pending) return 'Unavailable'
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Unavailable'
  return `$${value.toFixed(decimals)}`
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
  const [cacheStats, setCacheStats] = useState(null)
  const [billing, setBilling] = useState(previewBilling || null)
  const [loading, setLoading] = useState(!previewStats)
  const [error, setError]   = useState('')
  // Two independent failures, two independent lines. The 10s poll owns
  // `billingLoadError`; `goToStripe` owns `billingActionError`. Sharing one slot
  // meant the poll erased a "Stripe checkout failed" message seconds after the
  // user saw it, and one failed poll pinned its own red line for the rest of the
  // session even after every later poll succeeded.
  const [billingLoadError, setBillingLoadError] = useState('')
  const [billingActionError, setBillingActionError] = useState('')
  const [billingCheckedAt, setBillingCheckedAt] = useState('')
  const [billingStale, setBillingStale] = useState(false)
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
      const [data, cache] = await Promise.all([
        fetchStats(apiKey, { signal: controller.signal }),
        // The cache section renders only when this succeeds; a failure never blocks the page.
        fetchCacheStats(apiKey, { signal: controller.signal }).catch(() => null),
      ])
      if (controllerRef.current === controller) {
        setStats(data)
        setCacheStats(cache)
      }
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
      setBillingCheckedAt(new Date().toLocaleTimeString())
      setBillingStale(false)
      // Cleared on success only, so a failing poll never wipes its own explanation.
      setBillingLoadError('')
    } catch (e) {
      // The previous snapshot keeps rendering because a blank money panel is worse
      // than a labelled one -- but it has to be labelled as last-known, never passed
      // off as the current accrual.
      setBillingStale(true)
      setBillingLoadError(e.message)
    }
  }, [accessToken, previewBilling])

  useEffect(() => { loadBilling() }, [loadBilling, refreshTick])

  const goToStripe = async kind => {
    setBillingAction(kind)
    setBillingActionError('')
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
          setBillingActionError(portalError.message)
        }
      } else {
        setBillingActionError(e.message)
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

  // /v1/stats and /v1/stats/cache strip every *_usd key and set spend_redacted
  // for roles without billing access. Withheld money renders as "Withheld";
  // coercing the stripped keys with `|| 0` would show a confident wrong $0.00.
  const spendWithheld  = spendRedacted(stats)
  const cacheSpendWithheld = spendRedacted(cacheStats)
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
                  {billingActive ? 'active' : String(billing.subscription_status || 'unknown').replaceAll('_', ' ')}
                </span>
              )}
            </div>
            <h3 className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">25% of verified savings. Nothing else.</h3>
            <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-2 leading-relaxed">
              Stripe hosts card collection and the billing portal; Brevitas never receives card details. Usage is floored to micro-dollars, deduplicated{billing?.weekly_safety_cap_usd ? `, and constrained by a $${fmt(billing.weekly_safety_cap_usd, 0)} weekly safety cap` : ''}. Stripe closes and bills each metered period every seven days.
            </p>
            {billing && (
              <div className="flex flex-wrap gap-x-8 gap-y-2 mt-4 font-mono text-[11px] text-brand-muted dark:text-brand-dark-muted">
                <span>Accruing this week <strong className="text-brand-navy dark:text-brand-dark-navy">{fmtMoney(billing, billing.estimated_fee_usd)}</strong></span>
                <span>Reported to Stripe <strong className="text-brand-navy dark:text-brand-dark-navy">{fmtMoney(billing, billing.reported_fee_usd)}</strong></span>
                {billing.period_tracking_valid && <span>Billing week <strong className="text-brand-navy dark:text-brand-dark-navy">{fmtDate(billing.current_period_start)} → {fmtDate(billing.current_period_end)}</strong></span>}
              </div>
            )}
            {/* Money on screen states its own age. Without this the panel kept
                re-rendering the last snapshot as the live accrual for as long as
                /api/billing/status stayed down. */}
            {billing && billingCheckedAt && (billingStale
              ? <p className="font-mono text-[11px] text-amber-600 mt-2">Last updated {billingCheckedAt} — the latest refresh failed, so these amounts may be out of date.</p>
              : <p className="font-mono text-[11px] text-brand-muted dark:text-brand-dark-muted mt-2">Last updated {billingCheckedAt}.</p>)}
            {billingLoadError && <p className="font-mono text-xs text-red-500 mt-4">{billingLoadError}</p>}
            {billingActionError && <p className="font-mono text-xs text-red-500 mt-4">{billingActionError}</p>}
            {billingActive && billing && !billing.period_tracking_valid && <p className="font-mono text-xs text-red-500 mt-4">Weekly billing totals are unavailable because Stripe period boundaries have not synchronized. Charging is fail-closed for these entries.</p>}
            {billingActive && billing?.period_tracking_valid && billing.settlement_pending && <p className="font-mono text-xs text-amber-600 mt-4">Weekly totals are pending settlement. No amount is shown until the period of record is computed.</p>}
            {billingActive && billing?.period_tracking_valid && !billing.settlement_pending && billing.billable === false && <p className="font-mono text-xs text-amber-600 mt-4">This period is not billable pending a signed billing arrangement, so no fee will be charged for it.</p>}
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
          label="Input tokens avoided"
          value={fmtK(stats?.total_provider_input_tokens_avoided)}
          sub="actually not sent to providers"
        />
        <StatCard
          label="Provider spend"
          value={spendWithheld ? WITHHELD : allUnpriced ? 'Unpriced' : `$${fmt(providerSpend, 4)}`}
          sub="from provider receipts"
        />
        <StatCard
          label="Calls avoided"
          value={fmtK(stats?.total_calls_avoided)}
          sub="exact or opted-in response reuse"
          accent
        />
      </div>

      {/* Cache savings */}
      {cacheStats && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// cache savings</p>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard
              label="Cache hit rate"
              value={`${fmt(cacheStats.cache_hit_rate_pct)}%`}
              sub={`${fmtK(cacheStats.cached_input_tokens)} cached / ${fmtK(cacheStats.fresh_input_tokens)} fresh input tokens`}
            />
            <StatCard
              label="Native cache discount"
              value={cacheSpendWithheld ? WITHHELD : `$${fmt(cacheStats.native_cache_discount_usd, 4)}`}
              sub="measured across all cache reads"
            />
            <StatCard
              label="Billable cache savings"
              value={cacheSpendWithheld ? WITHHELD : `$${fmt(cacheStats.attributable_discount_usd, 4)}`}
              sub="Brevitas-attributable, byte-identical"
              accent
            />
            <StatCard
              label="Warming spend"
              value={cacheSpendWithheld ? WITHHELD : cacheStats.warm_spend_usd == null ? 'Not measured' : `$${fmt(cacheStats.warm_spend_usd, 4)}`}
              sub={cacheStats.warm_hits == null ? 'no warming data yet' : `${fmtK(cacheStats.warm_hits)} warm hits · ${fmtK(cacheStats.warm_pings)} pings`}
            />
          </div>
        </div>
      )}

      {/* This week */}
      {thisWeek && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// week of {thisWeek.week_start}</p>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Calls"         value={fmtK(thisWeek.calls)} />
            <StatCard label="Input tokens avoided" value={fmtK(thisWeek.provider_input_tokens_avoided)} />
            <StatCard label="Provider spend" value={spendWithheld ? WITHHELD : `$${fmt(thisWeek.actual_cost_usd, 4)}`} />
            <StatCard label="Verified savings" value={spendWithheld ? WITHHELD : `$${fmt(thisWeek.verified_savings_usd ?? thisWeek.cost_saved_usd, 4)}`} accent />
          </div>
        </div>
      )}

      {/* Weekly history table */}
      {weeks.length > 1 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// weekly usage history</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-x-auto">
            <div className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Week of', 'Calls', 'Input avoided', 'Provider spend', 'Verified benefit'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {weeks.map(m => (
              <div key={m.week_start} className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">{m.week_start}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.provider_input_tokens_avoided)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{spendWithheld ? WITHHELD : `$${fmt(m.actual_cost_usd, 4)}`}</span>
                <span className="font-mono text-xs text-brand-teal">{spendWithheld ? WITHHELD : `$${fmt(m.verified_savings_usd ?? m.cost_saved_usd, 4)}`}</span>
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
            ['Input avoided', 'Provider input tokens not sent after an input-reducing transform'],
            ['Native discount', 'Provider cache-read discount minus cache-write premiums; not automatically Brevitas-attributable'],
            ['Calls avoided', 'Model calls skipped by exact or explicitly enabled fuzzy response reuse'],
            ['Billable cache savings', 'Only reductions Brevitas provably caused with byte-identical responses — Brevitas-owned cache markers and exact replays; everything else is measured but not billed'],
            ['Transport avoided', 'Network bytes removed by CID/delta transport; provider tokens are unchanged'],
            ['Provider spend', 'What provider receipts say the optimized calls actually cost'],
            ['Brevitas vs control', 'Shown only when an isolated paired control arm was measured'],
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
