import { Fragment, useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats, fetchActivity } from '../lib/api.js'
import InstallCommand from './InstallCommand.jsx'
import {
  AreaChart, Area,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer,
} from 'recharts'

const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtWhen = (iso) => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
const fmtDay = (iso) => {
  const date = new Date(iso)
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (date.toDateString() === today.toDateString()) return 'today'
  if (date.toDateString() === yesterday.toDateString()) return 'yesterday'
  return date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
}
const groupSessionsByDay = (sessions) => {
  const days = []
  for (const session of sessions) {
    const key = new Date(session.started_at).toDateString()
    const bucket = days.find((d) => d.key === key)
    if (bucket) bucket.sessions.push(session)
    else days.push({ key, label: fmtDay(session.started_at), sessions: [session] })
  }
  return days
}
const fmtDuration = (secs) => {
  const s = Math.max(0, Number(secs) || 0)
  if (s < 60) return `${s}s`
  const m = Math.round(s / 60)
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`
}
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

export default function Overview({ apiKey, darkMode, refreshTick, previewStats = null, showInstallCommand = true }) {
  const [stats, setStats]     = useState(previewStats)
  const [activity, setActivity] = useState(null)
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
      const [data, act] = await Promise.all([
        fetchStats(apiKey, { signal: controller.signal }),
        fetchActivity(apiKey, { signal: controller.signal }).catch(() => null),
      ])
      if (controllerRef.current === controller) {
        setStats(data)
        setActivity(act)
      }
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
      const inputAvoided = Math.max(0, Number(h.provider_input_tokens_avoided) || 0)

      return {
        call: i + 1,
        inputAvoided,
        repo: h.repo || h.project || 'Unattributed',
      }
    })

  const recentAvoided = recentCalls.reduce((total, row) => total + row.inputAvoided, 0)
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
      {showInstallCommand && <InstallCommand phase="all" />}
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
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4">
        <BigStat value={stats.total_calls} label="// ai calls" />
        <BigStat value={fmt(stats.total_provider_input_tokens_avoided || 0)} label="// provider input tokens avoided" valueClass="text-brand-blue" />
        <BigStat value={fmt(stats.total_calls_avoided || 0)} label="// model calls avoided" valueClass="text-brand-blue" />
        <BigStat value={`$${Number(stats.total_native_cache_discount_usd || 0).toFixed(2)}`} label="// net native-cache discount" />
        <BigStat value={`$${Number(stats.total_actual_cost_usd || 0).toFixed(2)}`} label="// provider spend" />
        <BigStat
          value={stats.total_brevitas_incremental_savings_usd == null ? 'Not measured' : `$${Number(stats.total_brevitas_incremental_savings_usd).toFixed(2)}`}
          label="// Brevitas lift vs paired control"
        />
      </div>

      {/* ── Client activity ── */}
      {activity?.clients?.length > 0 && (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-8">
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2 mb-5">
            <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              client <em className="italic text-brand-blue">activity</em>
            </p>
            <p className="annotation">// a client counts as stopped after {activity.idle_minutes}m without calls</p>
          </div>
          <div className="space-y-2 mb-6">
            {activity.clients.map((c) => (
              <div key={c.client} className="flex flex-wrap items-center gap-3 rounded-xl border border-brand-border dark:border-brand-dark-border px-4 py-3">
                <span
                  className={`w-2 h-2 rounded-full shrink-0 ${c.active ? 'bg-emerald-500 animate-pulse' : 'bg-brand-border dark:bg-brand-dark-border'}`}
                  aria-hidden="true"
                />
                <span className="font-mono text-sm text-brand-navy dark:text-brand-dark-navy">{c.client}</span>
                <span className="annotation">
                  {c.active
                    ? `active now · last call ${fmtWhen(c.last_seen_at)}`
                    : `stopped · last used ${fmtWhen(c.last_seen_at)}`}
                </span>
                <span className="annotation ml-auto">{c.sessions} session{c.sessions === 1 ? '' : 's'} · {c.total_calls} calls</span>
              </div>
            ))}
          </div>
          {activity.sessions?.length > 0 && (
            <div>
              <p className="annotation tracking-widest uppercase mb-3">Past history</p>
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead>
                    <tr className="annotation">
                      <th className="font-normal pb-2 pr-4">client</th>
                      <th className="font-normal pb-2 pr-4">started</th>
                      <th className="font-normal pb-2 pr-4">stopped</th>
                      <th className="font-normal pb-2 pr-4">duration</th>
                      <th className="font-normal pb-2 text-right">calls</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">
                    {groupSessionsByDay(activity.sessions).map((day) => (
                      <Fragment key={day.key}>
                        <tr>
                          <td colSpan={5} className="annotation pt-4 pb-1">{day.label}</td>
                        </tr>
                        {day.sessions.map((s, i) => (
                          <tr key={i} className="border-t border-brand-border dark:border-brand-dark-border">
                            <td className="py-2 pr-4">{s.client}</td>
                            <td className="py-2 pr-4 tabular-nums">{fmtTime(s.started_at)}</td>
                            <td className="py-2 pr-4 tabular-nums">
                              {s.active ? <span className="text-emerald-500">active now</span> : fmtTime(s.last_seen_at)}
                            </td>
                            <td className="py-2 pr-4 tabular-nums">{fmtDuration(s.duration_seconds)}</td>
                            <td className="py-2 text-right tabular-nums">{s.calls}</td>
                          </tr>
                        ))}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

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
                provider input <em className="italic text-brand-blue">avoided</em> on each call
              </p>
              <p className="annotation mt-2">// excludes native cache discounts and transport-only savings</p>
            </div>
            <div className="min-w-0 sm:min-w-[180px]">
              <div className="rounded-xl border border-brand-blue/20 bg-brand-blue/5 dark:bg-brand-dark-blue-dim/40 px-4 py-3">
                <p className="annotation flex items-center gap-2">
                  <span className="w-5 h-0.5 rounded-full" style={{ backgroundColor: savedColor }} /> input avoided in range
                </p>
                <p className="font-mono text-xl sm:text-2xl text-brand-blue tabular-nums mt-1">{fmt(recentAvoided)}</p>
              </div>
            </div>
          </div>
          <div role="img" aria-label="Area chart showing provider input tokens avoided on each recent call">
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
                  dataKey="inputAvoided"
                  name="Input tokens avoided"
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
