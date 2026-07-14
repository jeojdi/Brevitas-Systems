import { useState, useEffect, useCallback } from 'react'

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

  const load = useCallback(async () => {
    if (!apiKey) return
    setError('')
    try {
      const r = await fetch('/v1/stats', { headers: { 'X-Brevitas-Key': apiKey } })
      if (!r.ok) {
        const error = await r.json().catch(() => ({}))
        throw new Error(error.detail || `Failed to load billing (${r.status})`)
      }
      setStats(await r.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { load() }, [load, refreshTick])

  if (!apiKey) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">// no API key — configure one in the Model tab</p>
    </div>
  )

  if (loading) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">Loading billing data…</p>
    </div>
  )

  if (error) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-red-500">{error}</p>
    </div>
  )

  const measuredSaved  = Number(stats?.total_measured_savings_usd || 0)
  const verifiedSaved  = Number(stats?.total_verified_savings_usd || 0)
  const totalFee       = Number(stats?.total_brevitas_fee_usd || 0)
  const months         = stats?.billing_by_month || []
  const thisMonth      = months[0] || null
  const allUnpriced    = Number(stats?.total_calls || 0) > 0 && Number(stats?.unpriced_calls || 0) === Number(stats?.total_calls || 0)

  return (
    <div className="space-y-10">
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Billing</p>
        <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          You save. We take 10%.
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-3 max-w-lg leading-relaxed">
          Brevitas charges 10% of the token cost you save by using compression.
          Nothing if it doesn't help.
        </p>
      </div>

      {/* All-time stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
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
        <StatCard
          label="Brevitas fee (total)"
          value={`$${fmt(totalFee, 4)}`}
          sub="10% of cost saved"
        />
      </div>

      {/* This month */}
      {thisMonth && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// this month — {thisMonth.month}</p>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Calls"         value={fmtK(thisMonth.calls)} />
            <StatCard label="Tokens saved"  value={fmtK(thisMonth.tokens_saved)} />
            <StatCard label="Cost saved"    value={`$${fmt(thisMonth.cost_saved_usd, 4)}`} accent />
            <StatCard label="Fee this month" value={`$${fmt(thisMonth.brevitas_fee_usd, 4)}`} />
          </div>
        </div>
      )}

      {/* Monthly history table */}
      {months.length > 1 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// monthly history</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-hidden">
            <div className="grid grid-cols-5 gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Month', 'Calls', 'Tokens saved', 'Cost saved', 'Fee'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {months.map(m => (
              <div key={m.month} className="grid grid-cols-5 gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">{m.month}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(m.tokens_saved)}</span>
                <span className="font-mono text-xs text-brand-teal">${fmt(m.cost_saved_usd, 4)}</span>
                <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">${fmt(m.brevitas_fee_usd, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty state */}
      {months.length === 0 && (
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-16 text-center">
          <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-2">No usage yet.</p>
          <p className="annotation">// start compressing to see billing data here</p>
        </div>
      )}

      {/* How billing works */}
      <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6 space-y-3">
        <p className="annotation tracking-widest uppercase mb-2">// how it works</p>
        <div className="space-y-2">
          {[
            ['Baseline tokens', 'What you would have sent to Anthropic/OpenAI without Brevitas'],
            ['Compressed tokens', 'What Brevitas actually sent after compression'],
            ['Tokens saved', 'The difference — real input tokens that never reached the provider'],
            ['Cost saved', 'Tokens saved × provider cost per token for your model'],
            ['Brevitas fee', '10% of cost saved — only charged when you save money'],
          ].map(([term, def]) => (
            <div key={term} className="flex gap-4">
              <span className="font-mono text-[11px] text-brand-blue shrink-0 w-36">{term}</span>
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
