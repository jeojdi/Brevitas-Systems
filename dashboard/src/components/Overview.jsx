import { Fragment, useState, useEffect, useCallback, useRef } from 'react'
import { fetchStats, fetchActivity, fetchCacheStats } from '../lib/api.js'
import { WITHHELD, spendRedacted } from '../lib/spend.js'
import InstallCommand from './InstallCommand.jsx'

const fmt = (n) => {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}
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
// Compact stat for the filtered "essentials" strip — deliberately smaller than the
// hero so it stays the focal point and the supporting numbers read as a secondary
// tier rather than the fourteen equal-weight cards this page used to show. The
// shimmer placeholder sits inside the real value element so it inherits the exact
// font-size/line box the number will occupy — data lands with zero layout shift.
// A small icon keyed off the client string, so each AI tool in the activity list is
// visually distinct. Claude/Claude Code uses the Anthropic mark; a few other common
// clients get a recognizable glyph; everything else falls back to a terminal icon.
function ClientIcon({ client }) {
  const c = String(client || '').toLowerCase()
  const cls = 'h-4 w-4 shrink-0'
  if (c.includes('claude') || c.includes('anthropic')) {
    return <img src="/assets/Claude%20Code%20Icon.svg" alt="" aria-hidden="true" className={cls} />
  }
  if (c.includes('openai') || c.includes('gpt') || c.includes('codex') || c.includes('chatgpt')) {
    return <img src="/assets/OpenAI%20Icon.svg" alt="" aria-hidden="true" className={cls} />
  }
  if (c.includes('copilot') || c.includes('github')) {
    return <svg className={cls} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 016 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58C20.56 22.29 24 17.8 24 12.5 24 5.87 18.63.5 12 .5z" /></svg>
  }
  if (c.includes('cursor')) {
    return <svg className={cls} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 2l16 8.5-7.2 1.6L10 20z" /></svg>
  }
  // Fallback: a generic terminal / CLI mark.
  return <svg className={cls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></svg>
}

function MiniStat({ value, label, valueClass = 'text-brand-navy dark:text-brand-dark-navy', pending = false }) {
  return (
    <div className="bg-white dark:bg-brand-dark-surface rounded-xl border border-brand-border dark:border-brand-dark-border px-4 py-3.5 min-w-0">
      <p className={`font-sans text-xl xl:text-2xl font-semibold tabular-nums truncate ${valueClass}`} title={pending ? undefined : String(value)}>
        {pending ? <span className="skeleton-text w-16" aria-hidden="true" /> : value}
      </p>
      <p className="mt-1.5 font-sans text-[11px] text-brand-muted dark:text-brand-dark-muted">{label}</p>
    </div>
  )
}

export default function Overview({ apiKey, darkMode, refreshTick, previewStats = null, showInstallCommand = true }) {
  const [stats, setStats]     = useState(previewStats)
  const [activity, setActivity] = useState(null)
  const [cacheStats, setCacheStats] = useState(null)
  const [loading, setLoading] = useState(!previewStats)
  const [error, setError]     = useState('')
  const controllerRef = useRef(null)

  const loadStats = useCallback(async () => {
    if (previewStats) {
      setStats(previewStats)
      setLoading(false)
      return
    }
    // App now mounts Overview before the workspace API key is minted (skeleton-first
    // shell), and clears the key on every user/workspace switch. Fetching with an
    // empty key would just 401, and letting the previous workspace's numbers linger
    // through a switch would show one workspace's data under another's name — so a
    // missing key drops back to the skeleton and waits. Nulling controllerRef (not
    // just aborting) keeps the aborted fetch's finally from flipping loading off and
    // killing the skeleton. `apiKey` is in this callback's deps, so the effect
    // re-runs and fetches the moment the key lands.
    if (!apiKey) {
      controllerRef.current?.abort()
      controllerRef.current = null
      setStats(null)
      setActivity(null)
      setCacheStats(null)
      setError('')
      setLoading(true)
      return
    }
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setError('')
    try {
      const [data, act, cache] = await Promise.all([
        fetchStats(apiKey, { signal: controller.signal }),
        fetchActivity(apiKey, { signal: controller.signal }).catch(() => null),
        fetchCacheStats(apiKey, { signal: controller.signal }).catch(() => null),
      ])
      if (controllerRef.current === controller) {
        setStats(data)
        setActivity(act)
        setCacheStats(cache)
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

  // Same component in both branches: React keeps OverviewBody mounted across the
  // pending→loaded transition, so numbers resolve into boxes that are already drawn.
  if (loading) return <OverviewBody pending showInstallCommand={showInstallCommand} darkMode={darkMode} loadStats={loadStats} />
  if (error && !stats) return <div className="pt-8"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={loadStats} className="annotation mt-3 hover:text-brand-blue">retry</button></div>
  // A re-run of loadStats clears `error` synchronously and never restores `loading`,
  // so a failed first load followed by the retry button or the 10s refresh tick lands
  // here with loading=false, error='', stats=null. Both guards above fall through and
  // the stat reads below would dereference null, unmounting the whole SPA.
  if (!stats) return <div className="pt-8"><p className="annotation">// stats unavailable</p><button onClick={loadStats} className="annotation mt-3 hover:text-brand-blue">retry</button></div>

  return (
    <OverviewBody
      showInstallCommand={showInstallCommand}
      darkMode={darkMode}
      loadStats={loadStats}
      error={error}
      stats={stats}
      activity={activity}
      cacheStats={cacheStats}
    />
  )
}

// One body for the skeleton and the loaded page: the pending state renders the same
// section headers, cards and chart shells with shimmer where values will land, so
// the placeholder layout cannot drift out of sync with the real one. Data-conditional
// sections (provider cache, client activity) stay hidden while pending because their
// existence is only known once the payloads arrive — pre-drawing them would shift the
// page when they turn out absent.
function OverviewBody({ pending = false, showInstallCommand, darkMode, loadStats, error = '', stats = null, activity = null, cacheStats = null }) {
  // Page index for the coffee-cup wall, plus a measured column count so each page is
  // exactly 4 rows regardless of viewport width (per-page = columns × 4).
  const [coffeePage, setCoffeePage] = useState(0)
  const [coffeeCols, setCoffeeCols] = useState(40)
  const coffeeRoRef = useRef(null)
  const coffeeRowRef = useCallback(node => {
    if (coffeeRoRef.current) { coffeeRoRef.current.disconnect(); coffeeRoRef.current = null }
    if (node) {
      const CUP_CELL_PX = 23 // ~cup width at h-8 plus the gap-0.5
      const measure = () => setCoffeeCols(Math.max(1, Math.floor(node.clientWidth / CUP_CELL_PX)))
      measure()
      coffeeRoRef.current = new ResizeObserver(measure)
      coffeeRoRef.current.observe(node)
    }
  }, [])
  // The API strips *_usd keys and sets spend_redacted for roles without billing
  // access; those sessions must see "Withheld", never a confident $0.00.
  const spendWithheld = spendRedacted(stats)
  const cacheSpendWithheld = spendRedacted(cacheStats)

  // Colors for the hit-rate ring gauge (the per-call area chart that consumed the
  // rest of the palette + recharts was removed).
  const gridColor = darkMode ? '#1c2440' : '#e2e4f0'
  const savedColor = '#2f2df5'

  // The one number the page leads with: dollars saved via native caching (or the
  // access-gated "Withheld" placeholder). Everything else is supporting.
  const heroSaved = spendWithheld
    ? WITHHELD
    : `$${Number(stats?.total_native_cache_discount_usd || 0).toFixed(2)}`

  // A playful translation of the savings into cups of coffee, drawn one cup per coffee
  // (plus a half cup for the leftover). COFFEE_PRICE is the assumed cost of one cup.
  const COFFEE_PRICE = 5
  const coffeeTotal = Number(stats?.total_native_cache_discount_usd || 0) / COFFEE_PRICE
  const coffeeFull = Math.floor(coffeeTotal)
  const coffeeHalf = coffeeTotal - coffeeFull >= 0.5

  // Verified savings, expressed as the share of the full (pre-cache) bill that the
  // measured native-cache discount removed: discount / (paid + discount). Both terms
  // come from provider receipts, so this is a receipt-verified percentage rather than
  // an estimate — and unlike verified_savings_usd / paired-control (both empty in
  // practice today) it is populated whenever there is any real spend.
  const actualCost = Number(stats?.total_actual_cost_usd || 0)
  const cacheDiscount = Number(stats?.total_native_cache_discount_usd || 0)
  const grossCost = actualCost + cacheDiscount
  const verifiedSavingsPct = grossCost > 0 ? (100 * cacheDiscount) / grossCost : 0

  // Hit-rate ring geometry. r/stroke are fixed; the arc is drawn by offsetting the
  // dash by the un-hit fraction of the full circumference (rotated to start at 12 o'clock).
  const hitPct = cacheStats ? Math.max(0, Math.min(100, Number(cacheStats.cache_hit_rate_pct || 0))) : 0
  const ringR = 62
  const ringC = 2 * Math.PI * ringR

  // Curated supporting metrics, filtered so a zero, an unmeasured value, or a
  // duplicate never occupies a full card. The native-cache discount is absent on
  // purpose — it is the hero above, and rendering it here too was the original
  // side-by-side duplication ("net native-cache discount" == "native cache discount").
  const essentials = pending
    ? [{}, {}, {}, {}]
    : [
        { value: fmt(stats?.total_calls ?? 0), label: '// ai calls' },
        { value: spendWithheld ? WITHHELD : `$${actualCost.toFixed(2)}`, label: '// cost after brevitas' },
        cacheStats && (cacheStats.fresh_input_tokens || 0) > 0 &&
          { value: fmt(cacheStats.fresh_input_tokens), label: '// tokens not cached' },
        { value: spendWithheld ? WITHHELD : `${verifiedSavingsPct.toFixed(1)}%`, label: '// verified savings', valueClass: 'text-brand-blue' },
        cacheStats && !cacheSpendWithheld && Number(cacheStats.attributable_discount_usd || 0) > 0 &&
          { value: `$${Number(cacheStats.attributable_discount_usd).toFixed(2)}`, label: '// Brevitas-attributable discount', valueClass: 'text-brand-blue' },
        (stats?.total_calls_avoided || 0) > 0 &&
          { value: fmt(stats.total_calls_avoided), label: '// model calls avoided', valueClass: 'text-brand-blue' },
        (stats?.total_provider_input_tokens_avoided || 0) > 0 &&
          { value: fmt(stats.total_provider_input_tokens_avoided), label: '// provider input tokens avoided', valueClass: 'text-brand-blue' },
      ].filter(Boolean)

  return (
    <div className="space-y-12" aria-busy={pending || undefined}>
      {showInstallCommand && <InstallCommand phase="all" />}
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={loadStats} className="annotation hover:text-brand-blue">retry</button></div>}

      {/* ── Hero: the one headline number, featured ── */}
      <div className="rounded-2xl border border-brand-border bg-white p-6 sm:p-8 dark:border-brand-dark-border dark:bg-brand-dark-surface">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <p className="font-sans text-3xl sm:text-4xl font-semibold text-brand-navy dark:text-brand-dark-navy mb-3">Saved through Brevitas</p>
            <p className="font-sans text-5xl sm:text-6xl font-semibold tabular-nums truncate text-brand-blue" title={pending ? undefined : heroSaved}>
              {pending ? <span className="skeleton-text w-48" aria-hidden="true" /> : heroSaved}
            </p>
            {!pending && cacheStats && (
              <div className="mt-4 flex flex-wrap gap-x-7 gap-y-1.5 font-sans text-sm text-brand-muted dark:text-brand-dark-muted">
                <span><strong className="text-brand-navy dark:text-brand-dark-navy tabular-nums">{fmt(cacheStats.cached_input_tokens || 0)}</strong> tokens cached</span>
                <span><strong className="text-brand-navy dark:text-brand-dark-navy tabular-nums">{fmt(stats?.total_calls ?? 0)}</strong> calls served</span>
              </div>
            )}
          </div>
          {/* Cache hit-rate ring — a single at-a-glance gauge that reads well with
              today's data (no dependence on weeks of trend history). */}
          {!pending && cacheStats && (
            <div className="flex w-full shrink-0 flex-col items-center gap-2 lg:w-64">
              <div className="relative h-[150px] w-[150px]" role="img" aria-label={`Cache hit rate ${hitPct.toFixed(2)} percent`}>
                <svg viewBox="0 0 150 150" className="h-full w-full -rotate-90">
                  <circle cx="75" cy="75" r={ringR} fill="none" stroke={gridColor} strokeWidth="13" />
                  <circle
                    cx="75" cy="75" r={ringR} fill="none"
                    stroke={savedColor} strokeWidth="13" strokeLinecap="round"
                    strokeDasharray={ringC}
                    strokeDashoffset={ringC * (1 - hitPct / 100)}
                    style={{ transition: 'stroke-dashoffset 700ms cubic-bezier(0.22,1,0.36,1)' }}
                  />
                </svg>
                <div className="absolute inset-0 flex items-center justify-center">
                  <span className="font-sans text-2xl font-semibold tabular-nums text-brand-navy dark:text-brand-dark-navy">{hitPct.toFixed(2)}%</span>
                </div>
              </div>
              <p className="text-center font-sans text-[11px] text-brand-muted dark:text-brand-dark-muted">cache hit rate</p>
            </div>
          )}
        </div>
        {/* Coffee equivalent — savings translated into cups, one image per coffee,
            paginated at 4 rows/page. Lives inside the hero card, below the divider. */}
        {!pending && !spendWithheld && coffeeTotal >= 0.5 && (() => {
          const COFFEE_PER_PAGE = coffeeCols * 4
          const coffeePages = Math.max(1, Math.ceil(coffeeFull / COFFEE_PER_PAGE))
          const page = Math.min(coffeePage, coffeePages - 1)
          const cupsOnPage = Math.min(COFFEE_PER_PAGE, coffeeFull - page * COFFEE_PER_PAGE)
          const halfOnPage = coffeeHalf && page === coffeePages - 1
          return (
            <div className="mt-6 border-t border-brand-border pt-5 dark:border-brand-dark-border">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <p className="font-sans text-sm text-brand-muted dark:text-brand-dark-muted">
                  That&apos;s about <strong className="text-brand-navy dark:text-brand-dark-navy tabular-nums">{coffeeHalf ? `${coffeeFull}½` : coffeeFull}</strong> coffees on us
                </p>
                {coffeePages > 1 && (
                  <div className="flex items-center gap-3">
                    <span className="annotation">Page {page + 1} of {coffeePages}</span>
                    <button type="button" disabled={page === 0} onClick={() => setCoffeePage(p => Math.max(0, p - 1))} className="annotation disabled:opacity-40 hover:text-brand-blue">Previous</button>
                    <button type="button" disabled={page >= coffeePages - 1} onClick={() => setCoffeePage(p => p + 1)} className="annotation disabled:opacity-40 hover:text-brand-blue">Next</button>
                  </div>
                )}
              </div>
              <div ref={coffeeRowRef} className="flex flex-wrap gap-0.5">
                {Array.from({ length: cupsOnPage }).map((_, i) => (
                  <img key={i} src="/assets/coffee-brevitas.png" alt="" aria-hidden="true" className="h-8 w-auto opacity-60" />
                ))}
                {halfOnPage && (
                  <span className="inline-flex h-8 w-[10px] overflow-hidden opacity-60" title="half a coffee">
                    <img src="/assets/coffee-brevitas.png" alt="" aria-hidden="true" className="h-8 max-w-none" />
                  </span>
                )}
              </div>
            </div>
          )
        })()}
      </div>

      {/* ── Essentials: a tight, filtered strip — no duplicates, no zero/unmeasured cards ── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
        {essentials.map((s, i) => (
          <MiniStat key={i} pending={pending} value={s.value} label={s.label} valueClass={s.valueClass} />
        ))}
      </div>

      {/* ── Client activity ── */}
      {activity?.clients?.length > 0 && (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-8">
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2 mb-5">
            <p className="font-sans text-2xl font-semibold text-brand-navy dark:text-brand-dark-navy">
              client activity
            </p>
          </div>
          <div className="space-y-2 mb-6">
            {activity.clients.map((c) => (
              <div key={c.client} className="flex flex-wrap items-center gap-3 rounded-xl border border-brand-border dark:border-brand-dark-border px-4 py-3">
                <span className="inline-flex items-center gap-2 font-mono text-sm text-brand-navy dark:text-brand-dark-navy"><ClientIcon client={c.client} />{c.client}</span>
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
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <thead>
                    <tr className="border-b border-brand-border dark:border-brand-dark-border font-mono text-[11px] font-semibold uppercase tracking-wider text-brand-navy-mid dark:text-brand-dark-navy-mid">
                      <th className="pb-2.5 pr-4">client</th>
                      <th className="pb-2.5 pr-4">started</th>
                      <th className="pb-2.5 pr-4">stopped</th>
                      <th className="pb-2.5 pr-4">duration</th>
                      <th className="pb-2.5 text-right">calls</th>
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
                            <td className="py-2 pr-4"><span className="inline-flex items-center gap-2"><ClientIcon client={s.client} />{s.client}</span></td>
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
    </div>
  )
}
