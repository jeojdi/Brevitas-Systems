import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats } from '../lib/api.js'
import InstallCommand from './InstallCommand.jsx'
import {
  AreaChart, Area,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer,
} from 'recharts'

const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtAxis = (n) => {
  const value = Number(n) || 0
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`
  if (value >= 1000) return `${Math.round(value / 1000)}k`
  return String(Math.round(value))
}

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
    <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-6 min-w-0">
      <p className={`font-mono text-3xl xl:text-4xl font-medium tabular-nums truncate ${valueClass}`} title={String(value)}>
        {value}
      </p>
      <p className="annotation mt-2">{label}</p>
    </div>
  )
}

export default function Overview({ apiKey, darkMode, refreshTick, previewStats = null }) {
  const [stats, setStats]     = useState(previewStats)
  const [loading, setLoading] = useState(!previewStats)
  const [error, setError]     = useState('')
  const controllerRef = useRef(null)

  const loadStats = useCallback(async () => {
    if (previewStats) {
      setStats(previewStats)
      setLoading(false)
      return
    }
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
    loadStats()
    return () => controllerRef.current?.abort()
  }, [loadStats, refreshTick])

  if (loading) return <p className="annotation pt-8">// loading…</p>
  if (error && !stats) return <div className="pt-8"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={loadStats} className="annotation mt-3 hover:text-brand-blue">retry</button></div>

  const recentCalls = [...(stats?.history ?? [])]
    .reverse()
    .slice(-20)
    .map((h, i) => {
      const baseline = Math.max(0, Number(h.baseline_tokens) || 0)
      const notSaved = Math.max(0, Number(h.optimized_tokens) || 0)
      const saved = Math.max(0, baseline - notSaved)

      return {
        call: i + 1,
        saved,
        notSaved,
        repo: h.repo || h.project || 'Unattributed',
      }
    })

  const recentSaved = recentCalls.reduce((total, row) => total + row.saved, 0)
  const chartData = recentCalls

  const gridColor    = darkMode ? '#1c2440' : '#e2e4f0'
  const tickColor    = darkMode ? '#576090' : '#8b93b8'
  const savedColor   = '#4f5fc4'
  const pointRingColor = darkMode ? '#141414' : '#ffffff'
  const tooltipStyle = getTooltipStyle(darkMode)
  const tooltipLabel = (call, payload) => {
    const repo = payload?.[0]?.payload?.repo
    return `Call #${call}${repo ? ` · ${repo}` : ''}`
  }

  return (
    <div className="space-y-12">
      <InstallCommand />
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={loadStats} className="annotation hover:text-brand-blue">retry</button></div>}
      {/* ── Section label ── */}
      <div>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-2">
          <div>
            <p className="annotation tracking-widest uppercase">Token efficiency overview</p>
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
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 sm:gap-4">
        <BigStat value={stats.total_calls} label="// ai calls" />
        <BigStat value={fmt(stats.total_tokens_saved)} label="// tokens saved" valueClass="text-brand-blue" />
        <BigStat value={`$${Number(stats.total_actual_cost_usd || 0).toFixed(2)}`} label="// provider spend" />
        <BigStat value={`$${Number(stats.total_verified_savings_usd || 0).toFixed(2)}`} label="// verified savings" valueClass="text-brand-teal" />
      </div>

      {chartData.length === 0 ? (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-10 sm:p-20 text-center">
          <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-3">No data yet.</p>
          <p className="annotation">
            // run a compression from the{' '}
            <span className="text-brand-navy dark:text-brand-dark-navy">Playground</span> tab to see charts
          </p>
        </div>
      ) : (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-4 sm:p-8 overflow-hidden">
          <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-5 mb-6">
            <div>
              <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
                tokens <em className="italic text-brand-blue">saved</em> on each call
              </p>
              <p className="annotation mt-2">// last {chartData.length} calls · based on provider receipts</p>
            </div>
            <div className="min-w-0 sm:min-w-[180px]">
              <div className="rounded-xl border border-brand-blue/20 bg-brand-blue/5 dark:bg-brand-dark-blue-dim/40 px-4 py-3">
                <p className="annotation flex items-center gap-2">
                  <span className="w-5 h-0.5 rounded-full" style={{ backgroundColor: savedColor }} /> saved in range
                </p>
                <p className="font-mono text-xl sm:text-2xl text-brand-blue tabular-nums mt-1">{fmt(recentSaved)}</p>
              </div>
            </div>
          </div>
          <div role="img" aria-label="Area chart showing tokens saved on each recent call">
            <ResponsiveContainer width="100%" height={320}>
              <AreaChart data={chartData} margin={{ top: 12, right: 8, left: 8, bottom: 4 }}>
                <defs>
                  <linearGradient id="savedArea" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={savedColor} stopOpacity={0.34} />
                    <stop offset="100%" stopColor={savedColor} stopOpacity={0.03} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} vertical={false} />
                <XAxis
                  dataKey="call"
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tick={{ fill: tickColor, fontSize: 11, fontFamily: 'JetBrains Mono' }}
                  tickFormatter={fmtAxis}
                  tickLine={false}
                  axisLine={false}
                  width={58}
                  domain={[0, 'auto']}
                  allowDecimals={false}
                />
                <Tooltip
                  {...tooltipStyle}
                  labelFormatter={tooltipLabel}
                  formatter={(value, name) => [`${Number(value).toLocaleString()} tokens`, name]}
                />
                <Area
                  type="monotone"
                  dataKey="saved"
                  name="Tokens saved"
                  stroke={savedColor}
                  fill="url(#savedArea)"
                  strokeWidth={3}
                  dot={{ r: 5.5, fill: savedColor, stroke: pointRingColor, strokeWidth: 2 }}
                  activeDot={{ r: 7.5, stroke: pointRingColor, strokeWidth: 2.5 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  )
}
