import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchAudit } from '../lib/api.js'

const fmtWhen = (iso) => {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

// Per-capability verdicts (brevitas.audit.v1). Anything unrecognized renders as
// "Not measured" — the audit never invents a claim the server did not make.
const CAPABILITY_VERDICTS = {
  already_implemented: {
    label: 'Already implemented',
    className: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  },
  partial: {
    label: 'Partial',
    className: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  },
  opportunity: {
    label: 'Opportunity',
    className: 'bg-brand-blue-dim text-brand-blue dark:bg-brand-dark-blue-dim',
  },
  not_applicable: {
    label: 'Not applicable',
    className: 'bg-brand-bg text-brand-muted dark:bg-brand-dark-bg dark:text-brand-dark-muted',
  },
  unknown: {
    label: 'Not measured',
    className: 'bg-brand-bg text-brand-muted dark:bg-brand-dark-bg dark:text-brand-dark-muted',
  },
}

const IMPACT_LABELS = { high: 'high', medium: 'medium', low: 'low' }

// Top-level report verdicts. Anything unrecognized — including the
// "not_measured" the server emits when zero capabilities were measurable
// (every new key before traffic flows) — falls back to the no-claim state:
// the audit never invents a savings claim the server did not make.
const REPORT_VERDICTS = {
  well_optimized: {
    headline: 'You are already well-optimized.',
    subline: 'Every applicable optimization we checked is already in place. There is nothing meaningful left for Brevitas to add here — and we would rather tell you that than invent an opportunity.',
  },
  high_opportunity: {
    headline: 'Significant savings are still on the table.',
    subline: 'Most applicable optimizations are not in place yet. Each opportunity below lists the evidence behind the claim.',
  },
  moderate_opportunity: {
    headline: 'Some savings are still on the table.',
    subline: 'A few applicable optimizations are not in place yet. Each opportunity below lists the evidence behind the claim.',
  },
  not_measured: {
    headline: 'Nothing measured yet.',
    subline: 'No proxied traffic has been recorded for this key, so the audit has nothing to measure — and makes no claims either way. Route traffic through Brevitas or run the static scanner (brevitas audit <repo>) to get a real verdict.',
  },
}

function VerdictBadge({ verdict }) {
  const { label, className } = CAPABILITY_VERDICTS[verdict] || CAPABILITY_VERDICTS.unknown
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider ${className}`}>
      {label}
    </span>
  )
}

function ScoreTile({ score, verdict }) {
  const wellOptimized = verdict === 'well_optimized'
  // A report that measured nothing has no score — a "0" here would read as
  // "fully optimized", a claim the server never made.
  const measured = REPORT_VERDICTS[verdict] && verdict !== 'not_measured'
  const display = measured && Number.isFinite(Number(score)) ? Math.round(Number(score)) : '—'
  return (
    <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-6 min-w-0">
      <p
        className={`font-mono text-3xl xl:text-4xl font-medium tabular-nums ${
          wellOptimized
            ? 'text-brand-teal'
            : measured
              ? 'text-brand-blue'
              : 'text-brand-muted dark:text-brand-dark-muted'
        }`}
        title={measured ? String(score) : 'not measured'}
      >
        {display}
      </p>
      <p className="annotation mt-2">
        {measured
          ? '// opportunity score · 0 = fully optimized · 100 = greenfield'
          : '// opportunity score · not measured yet'}
      </p>
    </div>
  )
}

export default function Audit({ apiKey, refreshTick }) {
  const [report, setReport]   = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')
  const [unavailable, setUnavailable] = useState(false)
  const controllerRef = useRef(null)

  const loadAudit = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setError('')
    try {
      const data = await fetchAudit(apiKey, { signal: controller.signal })
      if (controllerRef.current === controller) {
        setReport(data)
        setUnavailable(false)
      }
    } catch (e) {
      if (controllerRef.current !== controller || e.name === 'AbortError') return
      // The audit endpoint ships server-side later on some deployments;
      // a missing route is a zero-state, not a failure.
      if ([404, 501].includes(e.status)) setUnavailable(true)
      else setError(e.message)
    } finally {
      if (controllerRef.current === controller) setLoading(false)
    }
  }, [apiKey])

  useEffect(() => {
    loadAudit()
    return () => controllerRef.current?.abort()
  }, [loadAudit, refreshTick])

  if (loading) return <p className="annotation pt-8">// loading…</p>

  if (unavailable && !report) {
    return (
      <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-10 sm:p-20 text-center">
        <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-3">No audit available yet.</p>
        <p className="annotation">
          // the optimization audit is not enabled on this deployment — nothing is measured, so nothing is claimed
        </p>
      </div>
    )
  }

  if (error && !report) {
    return (
      <div className="pt-8">
        <p className="font-mono text-xs text-red-500">{error}</p>
        <button onClick={loadAudit} className="annotation mt-3 hover:text-brand-blue">retry</button>
      </div>
    )
  }

  const capabilities = Array.isArray(report?.capabilities) ? report.capabilities : []
  const caveats = Array.isArray(report?.caveats) ? report.caveats : []
  const providers = Array.isArray(report?.providers_detected) ? report.providers_detected : []
  const wellOptimized = report?.verdict === 'well_optimized'
  const scannedAt = report?.scanned_at ? fmtWhen(report.scanned_at) : ''

  const { headline, subline } = REPORT_VERDICTS[report?.verdict] || REPORT_VERDICTS.not_measured

  return (
    <div className="space-y-12">
      {error && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4">
          <p className="font-mono text-xs text-red-500">{error}</p>
          <button onClick={loadAudit} className="annotation hover:text-brand-blue">retry</button>
        </div>
      )}

      {/* ── Section label ── */}
      <div>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-2">
          <div>
            <p className="annotation tracking-widest uppercase">Optimization audit</p>
            <p className="annotation mt-1">
              // {report?.source === 'traffic' ? 'measured from live proxied traffic' : 'static scan of your integration'}
              {scannedAt ? ` · scanned ${scannedAt}` : ''}
            </p>
          </div>
          <button onClick={loadAudit} className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors">
            refresh now
          </button>
        </div>
        <div className="h-px bg-brand-border dark:bg-brand-dark-border" />
      </div>

      {/* ── Verdict headline ── */}
      <div className={`rounded-2xl border p-6 sm:p-8 ${
        wellOptimized
          ? 'border-brand-teal/30 bg-emerald-500/5'
          : 'border-brand-border bg-white dark:border-brand-dark-border dark:bg-brand-dark-surface'
      }`}>
        <div className="flex flex-col gap-6 lg:flex-row lg:items-start lg:justify-between">
          <div className="max-w-2xl">
            <p className={`font-serif text-3xl sm:text-4xl ${wellOptimized ? 'text-brand-teal' : 'text-brand-navy dark:text-brand-dark-navy'}`}>
              {headline}
            </p>
            <p className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid">{subline}</p>
            {providers.length > 0 && (
              <p className="annotation mt-4">
                // providers detected: {providers.join(', ')}
              </p>
            )}
          </div>
          <div className="w-full lg:w-64 shrink-0">
            <ScoreTile score={report?.score} verdict={report?.verdict} />
          </div>
        </div>
      </div>

      {/* ── Capability table ── */}
      {capabilities.length > 0 && (
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-8">
          <div className="mb-5">
            <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              capability <em className="italic text-brand-blue">breakdown</em>
            </p>
            <p className="annotation mt-2">// what you already do yourself, and what is genuinely left</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="annotation">
                  <th className="font-normal pb-2 pr-4">capability</th>
                  <th className="font-normal pb-2 pr-4">verdict</th>
                  <th className="font-normal pb-2 pr-4">impact</th>
                  <th className="font-normal pb-2">evidence</th>
                </tr>
              </thead>
              <tbody className="text-xs text-brand-navy dark:text-brand-dark-navy">
                {capabilities.map((capability) => {
                  const evidence = Array.isArray(capability.evidence) ? capability.evidence : []
                  return (
                    <tr key={capability.id || capability.name} className="border-t border-brand-border dark:border-brand-dark-border align-top">
                      <td className="py-3 pr-4 font-mono">{capability.name || capability.id}</td>
                      <td className="py-3 pr-4"><VerdictBadge verdict={capability.verdict} /></td>
                      <td className="py-3 pr-4 font-mono">
                        {IMPACT_LABELS[capability.estimated_impact] || '—'}
                      </td>
                      <td className="py-3">
                        {capability.detail && (
                          <p className="leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid">{capability.detail}</p>
                        )}
                        {evidence.length > 0 && (
                          <ul className="mt-1 space-y-1">
                            {evidence.map((item, index) => (
                              <li key={index} className="annotation">// {item}</li>
                            ))}
                          </ul>
                        )}
                        {!capability.detail && evidence.length === 0 && <span className="annotation">—</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Caveats footer ── */}
      {caveats.length > 0 && (
        <div className="rounded-2xl border border-brand-border dark:border-brand-dark-border p-5 sm:p-6">
          <p className="annotation tracking-widest uppercase mb-3">Caveats</p>
          <ul className="space-y-2">
            {caveats.map((caveat, index) => (
              <li key={index} className="annotation leading-relaxed">// {caveat}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
