import { useState, useEffect, useCallback } from 'react'
import {
  BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip,
  Legend, ResponsiveContainer,
} from 'recharts'

const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const REPO_COLORS = ['#4f5fc4', '#2d8a6e', '#d97706', '#be185d', '#7c3aed', '#0891b2']

function getTooltipStyle(dark) {
  return {
    contentStyle: {
      backgroundColor: dark ? '#141a2e' : '#ffffff',
      border: `1px solid ${dark ? '#1c2440' : '#e2e4f0'}`,
      borderRadius: 10,
      fontSize: 12,
      color: dark ? '#dde2f8' : '#0d1530',
      boxShadow: dark
        ? '0 4px 16px rgba(0,0,0,0.4)'
        : '0 4px 16px rgba(13,21,48,0.08)',
    },
    labelStyle: { color: dark ? '#576090' : '#8b93b8', fontFamily: 'JetBrains Mono' },
  }
}

function BigStat({ value, label, valueClass = 'text-brand-navy dark:text-brand-dark-navy' }) {
  return (
    <div className="text-center">
      <p className={`font-mono text-4xl lg:text-5xl font-medium tabular-nums ${valueClass}`}>
        {value}
      </p>
      <p className="annotation mt-2">{label}</p>
    </div>
  )
}

export default function Overview({ apiKey, darkMode, refreshTick }) {
  const [stats, setStats]     = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')

  const loadStats = useCallback(async () => {
    setError('')
    try {
      const res = await fetch('/v1/stats', { headers: { 'X-Brevitas-Key': apiKey } })
      if (!res.ok) throw new Error('Failed to load stats')
      setStats(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { loadStats() }, [loadStats, refreshTick])

  if (loading) return <p className="annotation pt-8">// loading…</p>
  if (error)   return <p className="font-mono text-xs text-red-500 pt-8">{error}</p>

  const chartData = [...(stats?.history ?? [])]
    .reverse()
    .slice(-20)
    .map((h, i) => ({
      call: i + 1,
      savings:   parseFloat(h.savings_pct.toFixed(1)),
      baseline:  h.baseline_tokens,
      optimized: h.optimized_tokens,
      repo:       h.repo || h.project || 'Unattributed',
    }))

  const repos = [...new Set(chartData.map(row => row.repo))]
  const repoColors = Object.fromEntries(repos.map((repo, index) => [repo, REPO_COLORS[index % REPO_COLORS.length]]))

  const gridColor    = darkMode ? '#1c2440' : '#e2e4f0'
  const tickColor    = darkMode ? '#576090' : '#8b93b8'
  const labelColor   = darkMode ? '#2e3860' : '#c4c8e2'
  const baselineFill = darkMode ? '#1c2440' : '#e2e4f0'
  const tooltipStyle = getTooltipStyle(darkMode)

  return (
    <div className="space-y-16">
      {/* ── Section label ── */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div>
            <p className="annotation tracking-widest uppercase">Dashboard metrics — 2026</p>
            <p className="annotation mt-1">Tracking runs server-side, even when this dashboard is closed.</p>
          </div>
          <button
            onClick={loadStats}
            className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
          >
            refresh now
          </button>
        </div>
        <div className="h-px bg-brand-border dark:bg-brand-dark-border" />
      </div>

      {/* ── Big stats row ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-8 lg:gap-4">
        <BigStat value={stats.total_calls} label="// ai calls" />
        <BigStat value={fmt(stats.total_tokens_saved)} label="// tokens saved" valueClass="text-brand-blue" />
        <BigStat value={`$${Number(stats.total_measured_savings_usd || 0).toFixed(2)}`} label="// measured savings" valueClass="text-brand-blue" />
        <BigStat value={`$${Number(stats.total_verified_savings_usd || 0).toFixed(2)}`} label="// verified savings" valueClass="text-brand-teal" />
      </div>

      {/* ── Token flow summary ── */}
      {stats.total_calls > 0 && (
        <div className="text-center space-y-2">
          <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid">
            {fmt(stats.total_actual_tokens)} tokens consumed,{' '}
            <em className="font-serif italic text-brand-blue">{fmt(stats.total_tokens_saved)} saved.</em>
          </p>
          <p className="annotation">// provider receipts · {stats.unpriced_calls || 0} unpriced calls</p>
        </div>
      )}

      {chartData.length === 0 ? (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-20 text-center">
          <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-3">No data yet.</p>
          <p className="annotation">
            // run a compression from the{' '}
            <span className="text-brand-navy dark:text-brand-dark-navy">Playground</span> tab to see charts
          </p>
        </div>
      ) : (
        <>
          {/* ── Savings chart ── */}
          <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-8">
            <p className="annotation tracking-widest uppercase mb-1">Savings %</p>
            <p className="font-serif text-xl text-brand-navy dark:text-brand-dark-navy mb-6">
              last {chartData.length} calls
            </p>
            <div className="flex flex-wrap gap-x-5 gap-y-2 mb-4">
              {repos.map(repo => (
                <span key={repo} className="annotation flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: repoColors[repo] }} />
                  {repo}
                </span>
              ))}
            </div>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 16 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="call"
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  label={{ value: 'call #', position: 'insideBottom', offset: -10, fill: labelColor, fontSize: 10, fontFamily: 'JetBrains Mono' }}
                />
                <YAxis
                  domain={[0, 100]}
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  tickFormatter={v => `${v}%`}
                />
                <Tooltip
                  {...tooltipStyle}
                  formatter={(value, _name, { payload }) => [`${value}%`, payload.repo]}
                />
                <Bar
                  dataKey="savings"
                  name="savings"
                  radius={[4, 4, 0, 0]}
                >
                  {chartData.map((row, index) => <Cell key={index} fill={repoColors[row.repo]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* ── Token comparison chart ── */}
          <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-8">
            <p className="annotation tracking-widest uppercase mb-1">Token footprint</p>
            <p className="font-serif text-xl text-brand-navy dark:text-brand-dark-navy mb-6">
              baseline <em className="italic text-brand-blue">vs</em> optimized
            </p>
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={chartData} barGap={2} margin={{ top: 4, right: 4, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
                <XAxis
                  dataKey="call"
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                />
                <YAxis
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  tickFormatter={fmt}
                />
                <Tooltip {...tooltipStyle} />
                <Legend
                  wrapperStyle={{ fontSize: 11, color: tickColor, fontFamily: 'JetBrains Mono' }}
                />
                <Bar dataKey="baseline"  name="baseline"  fill={baselineFill} radius={[4, 4, 0, 0]} />
                <Bar dataKey="optimized" name="optimized" fill="#4f5fc4"      radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  )
}
