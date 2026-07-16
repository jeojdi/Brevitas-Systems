import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats } from '../lib/api.js'

function fmt(n, decimals = 2) {
  return Number(n || 0).toFixed(decimals)
}
function fmtK(n) {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
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

export default function Billing({ apiKey, refreshTick }) {
  const [stats, setStats]   = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]   = useState('')
  const controllerRef = useRef(null)

  const load = useCallback(async () => {
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
  }, [apiKey])

  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load, refreshTick])

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

  const measuredSaved  = Number(stats?.total_measured_savings_usd || 0)
  const verifiedSaved  = Number(stats?.total_verified_savings_usd || 0)
  const months         = stats?.billing_by_month || []
  const thisMonth      = months[0] || null
  const allUnpriced    = Number(stats?.total_calls || 0) > 0 && Number(stats?.unpriced_calls || 0) === Number(stats?.total_calls || 0)

  return (
    <div className="space-y-10" data-ph-sensitive>
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Savings</p>
        <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          Track what Brevitas saves.
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-3 max-w-lg leading-relaxed">
          Token and cost estimates from your recorded provider usage. Unpriced calls remain visible without a guessed dollar value.
        </p>
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
          label="Measured savings"
          value={allUnpriced ? 'Unpriced' : `$${fmt(measuredSaved, 4)}`}
          sub="receipt-based estimate"
          accent
        />
        <StatCard
          label="Verified savings"
          value={`$${fmt(verifiedSaved, 4)}`}
          sub="passed the quality gate"
          accent
        />
      </div>

      {/* This month */}
      {thisMonth && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// this month — {thisMonth.month}</p>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Calls"         value={fmtK(thisMonth.calls)} />
            <StatCard label="Tokens saved"  value={fmtK(thisMonth.tokens_saved)} />
            <StatCard label="Measured savings" value={`$${fmt(thisMonth.measured_savings_usd ?? thisMonth.cost_saved_usd, 4)}`} accent />
            <StatCard label="Verified savings" value={`$${fmt(thisMonth.verified_savings_usd ?? thisMonth.cost_saved_usd, 4)}`} accent />
          </div>
        </div>
      )}

      {/* Monthly history table */}
      {months.length > 1 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// monthly history</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-x-auto">
            <div className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Month', 'Calls', 'Tokens saved', 'Measured', 'Verified'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {months.map(m => (
              <div key={m.month} className="grid grid-cols-5 min-w-[620px] gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">{m.month}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.tokens_saved)}</span>
                <span className="font-mono text-xs text-brand-blue">${fmt(m.measured_savings_usd ?? m.cost_saved_usd, 4)}</span>
                <span className="font-mono text-xs text-brand-teal">${fmt(m.verified_savings_usd ?? m.cost_saved_usd, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {months.length === 0 && (
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
            ['Measured savings', 'Receipt-based estimate using the recorded provider and model'],
            ['Verified savings', 'Measured savings from calls that passed the quality gate'],
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
