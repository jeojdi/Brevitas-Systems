import { useEffect, useMemo, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'

const num = value => Number(value || 0).toLocaleString()
const usd = value => `$${Number(value || 0).toFixed(4)}`
const billingUsd = value => `$${Number(value || 0).toFixed(2)}`
const duration = seconds => seconds >= 60 ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s` : `${Math.round(seconds)}s`
const ranges = ['7d', '30d', '90d', 'all']

function StatCard({ label, value, accent = '' }) {
  return <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5">
    <p className="annotation">{label}</p>
    <p className={`font-serif text-2xl sm:text-3xl mt-2 ${accent || 'text-brand-navy dark:text-brand-dark-navy'}`}>{value}</p>
  </div>
}

async function adminJson(path, accessToken, signal) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${accessToken}` }, signal })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || (response.status === 403 ? 'Admin access denied' : `Request failed (${response.status})`))
  }
  return response.json()
}

export default function Admin({ accessToken, refreshTick }) {
  const [range, setRange] = useState('30d')
  const [filters, setFilters] = useState({ account: '', project: '', client: '', provider: '', model: '' })
  const [data, setData] = useState(null)
  const [traffic, setTraffic] = useState(null)
  const [error, setError] = useState('')
  const [trafficError, setTrafficError] = useState('')
  const [offset, setOffset] = useState(0)
  const [billingOpen, setBillingOpen] = useState(false)
  const [billing, setBilling] = useState(null)
  const [billingError, setBillingError] = useState('')

  const query = useMemo(() => {
    const params = new URLSearchParams({ range, limit: '100', offset: String(offset) })
    Object.entries(filters).forEach(([key, value]) => { if (value.trim()) params.set(key, value.trim()) })
    return params.toString()
  }, [range, filters, offset])

  const billingQuery = useMemo(() => {
    const params = new URLSearchParams({ range })
    Object.entries(filters).forEach(([key, value]) => { if (value.trim()) params.set(key, value.trim()) })
    return params.toString()
  }, [range, filters])

  useEffect(() => {
    const controller = new AbortController()
    setError('')
    adminJson(`/v1/admin/stats/breakdown?${query}`, accessToken, controller.signal)
      .then(setData)
      .catch(error => { if (error.name !== 'AbortError') setError(error.message) })
    return () => controller.abort()
  }, [accessToken, query, refreshTick])

  useEffect(() => {
    const controller = new AbortController()
    const trafficRange = range === 'all' ? '90d' : range
    setTrafficError('')
    adminJson(`/v1/admin/analytics?range=${trafficRange}`, accessToken, controller.signal)
      .then(setTraffic)
      .catch(error => { if (error.name !== 'AbortError') setTrafficError(error.message) })
    return () => controller.abort()
  }, [accessToken, range, refreshTick])

  useEffect(() => {
    if (!billingOpen) return undefined
    const controller = new AbortController()
    setBillingError('')
    adminJson(`/v1/admin/billing?${billingQuery}`, accessToken, controller.signal)
      .then(setBilling)
      .catch(error => { if (error.name !== 'AbortError') setBillingError(error.message) })
    return () => controller.abort()
  }, [accessToken, billingOpen, billingQuery, refreshTick])

  const updateFilter = (key, value) => {
    setOffset(0)
    setFilters(current => ({ ...current, [key]: value }))
  }

  if (error && !data) return <p className="font-mono text-xs text-red-500">{error}</p>
  if (!data) return <p className="annotation">// loading admin operations…</p>

  const totals = data.totals || {}
  const page = data.pagination || { total: data.rows.length, limit: 100, offset: 0 }

  return <div className="space-y-10 ph-no-capture" data-ph-sensitive>
    <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-5">
      <div>
        <p className="annotation tracking-widest uppercase">Brevitas operations · restricted</p>
        <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">Customer and traffic intelligence.</h2>
        <p className="text-brand-muted mt-3 max-w-3xl">Financial receipts and masked web analytics. Prompts, responses, code, paths, provider keys, and network bodies are excluded.</p>
      </div>
      <div className="flex gap-2" aria-label="Reporting period">
        {ranges.map(value => <button key={value} onClick={() => { setRange(value); setOffset(0) }}
          className={`px-3 py-2 rounded-xl font-mono text-xs ${range === value ? 'bg-brand-blue text-white' : 'border border-brand-border dark:border-brand-dark-border text-brand-muted'}`}>{value}</button>)}
      </div>
    </div>

    <section className="space-y-4">
      <div className="flex items-center justify-between gap-4"><div><p className="annotation tracking-widest uppercase">Site traffic</p><h3 className="font-serif text-2xl mt-1">PostHog summary.</h3></div>{traffic?.posthog_url && <a href={traffic.posthog_url} target="_blank" rel="noreferrer" className="text-xs text-brand-blue">Open detailed analytics ↗</a>}</div>
      {trafficError ? <div className="rounded-xl border border-amber-300/40 p-4 text-xs text-amber-600">{trafficError}. Financial reporting remains available.</div> : !traffic ? <p className="annotation">// loading traffic…</p> : <>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard label="Unique visitors" value={num(traffic.visitors)} />
          <StatCard label="Sessions" value={num(traffic.sessions)} />
          <StatCard label="Average duration" value={duration(traffic.avg_session_duration_seconds)} />
          <StatCard label="Bounce rate" value={`${Number(traffic.bounce_rate || 0).toFixed(1)}%`} />
        </div>
        <div className="grid lg:grid-cols-[2fr_1fr] gap-4">
          <div className="h-72 bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5" data-ph-sensitive>
            <ResponsiveContainer width="100%" height="100%"><LineChart data={traffic.trend}><CartesianGrid strokeDasharray="3 3" stroke="#e2e4f0"/><XAxis dataKey="date" tick={{ fontSize: 10 }}/><YAxis tick={{ fontSize: 10 }}/><Tooltip/><Line type="monotone" dataKey="visitors" stroke="#4f5fc4" strokeWidth={2} dot={false}/><Line type="monotone" dataKey="pageviews" stroke="#2d8a6e" strokeWidth={2} dot={false}/></LineChart></ResponsiveContainer>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-1 gap-4"><StatCard label="Pageviews" value={num(traffic.pageviews)} /><StatCard label="Signup submitted" value={`${num(traffic.signup_submitted)} / ${num(traffic.signup_started)} started`} accent="text-brand-teal" /></div>
        </div>
      </>}
    </section>

    <section className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div><p className="annotation tracking-widest uppercase">Financial operations</p><h3 className="font-serif text-2xl mt-1">Customer spend and savings.</h3></div>
        <button type="button" aria-expanded={billingOpen} onClick={() => setBillingOpen(open => !open)}
          className="rounded-xl bg-brand-blue text-white px-4 py-2.5 text-sm font-medium">
          {billingOpen ? 'Hide billing' : 'Billing · Amount owed'}
        </button>
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Actual customer spend" value={usd(totals.total_actual_cost_usd)} />
        <StatCard label="Baseline spend" value={usd(totals.total_baseline_cost_usd)} />
        <StatCard label="Verified savings" value={usd(totals.total_verified_savings_usd)} accent="text-brand-teal" />
        <StatCard label="Brevitas fees" value={usd(totals.total_brevitas_fee_usd)} accent="text-brand-blue" />
      </div>

      {billingOpen && <div className="rounded-2xl border border-brand-blue/30 bg-brand-blue/5 p-5 space-y-5 ph-no-capture" data-ph-sensitive>
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <p className="annotation tracking-widest uppercase">Billing · restricted</p>
            <h4 className="font-serif text-3xl mt-1 text-brand-navy dark:text-brand-dark-navy">Amount owed to Brevitas</h4>
            <p className="text-sm text-brand-muted mt-2">Calculated from metered Brevitas fees for the selected period and filters. Payment and collection status are not yet tracked.</p>
          </div>
          <p className="font-serif text-4xl text-brand-blue">{billing ? billingUsd(billing.amount_owed_usd) : '—'}</p>
        </div>
        {billingError ? <p className="font-mono text-xs text-red-500">{billingError}</p> : !billing ? <p className="annotation">// loading billing…</p> :
          <div className="overflow-x-auto rounded-xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
            <table className="w-full min-w-[720px] text-left"><thead><tr>{['Account', 'Calls', 'Customer spend', 'Verified savings', 'Amount owed'].map(label => <th key={label} className="annotation px-4 py-3 border-b border-brand-border dark:border-brand-dark-border">{label}</th>)}</tr></thead>
              <tbody>{billing.accounts.length ? billing.accounts.map(account => <tr key={account.account_id} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
                <td className="font-mono text-xs px-4 py-3 ph-no-capture" data-ph-sensitive>{account.account_email || 'No email'}<br/><span className="text-brand-muted">{account.account_id}</span></td>
                <td className="font-mono text-xs px-4 py-3">{num(account.calls)}</td>
                <td className="font-mono text-xs px-4 py-3">{billingUsd(account.actual_spend_usd)}</td>
                <td className="font-mono text-xs px-4 py-3 text-brand-teal">{billingUsd(account.verified_savings_usd)}</td>
                <td className="font-mono text-xs px-4 py-3 text-brand-blue">{billingUsd(account.amount_owed_usd)}</td>
              </tr>) : <tr><td colSpan="5" className="annotation px-4 py-5">No billable usage for these filters.</td></tr>}</tbody></table>
          </div>}
      </div>}

      <div className="grid sm:grid-cols-2 lg:grid-cols-5 gap-3">
        {Object.keys(filters).map(key => <label key={key} className="annotation capitalize">{key}
          <input value={filters[key]} onChange={event => updateFilter(key, event.target.value)} placeholder={`Filter ${key}`}
            className="mt-1 w-full rounded-xl border border-brand-border dark:border-brand-dark-border px-3 py-2 text-sm text-brand-navy dark:text-brand-dark-navy" data-ph-sensitive />
        </label>)}
      </div>
      {error && <p className="font-mono text-xs text-red-500">{error}</p>}
      <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
        <table className="w-full min-w-[1180px] text-left"><thead><tr>{['Account', 'Project / client', 'Provider / model', 'Calls', 'Tokens saved', 'Actual spend', 'Baseline', 'Verified savings', 'Brevitas fee'].map(label => <th key={label} className="annotation px-4 py-3 border-b border-brand-border dark:border-brand-dark-border">{label}</th>)}</tr></thead>
          <tbody>{data.rows.map((row, index) => <tr key={`${row.account_id}-${row.project}-${row.client}-${row.model}-${index}`} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
            <td className="font-mono text-xs px-4 py-3 ph-no-capture" data-ph-sensitive><span>{row.account_email || 'No email'}</span><br/><span className="text-brand-muted">{row.account_id}</span></td>
            <td className="font-mono text-xs px-4 py-3">{row.project}<br/><span className="text-brand-muted">{row.client || row.source}</span></td>
            <td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.provider}<br/><span>{row.model}</span></td>
            <td className="font-mono text-xs px-4 py-3">{num(row.calls)}</td><td className="font-mono text-xs px-4 py-3">{num(row.tokens_saved)}</td>
            <td className="font-mono text-xs px-4 py-3">{usd(row.actual_cost_usd)}</td><td className="font-mono text-xs px-4 py-3">{usd(row.baseline_cost_usd)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-teal">{usd(row.verified_savings_usd)}</td><td className="font-mono text-xs px-4 py-3 text-brand-blue">{usd(row.brevitas_fee_usd)}</td>
          </tr>)}</tbody></table>
      </div>
      <div className="flex items-center justify-between"><p className="annotation">{page.total ? `${page.offset + 1}–${Math.min(page.offset + page.limit, page.total)} of ${page.total}` : 'No matching rows'}</p><div className="flex gap-2"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))} className="annotation disabled:opacity-40">Previous</button><button disabled={offset + page.limit >= page.total} onClick={() => setOffset(offset + 100)} className="annotation disabled:opacity-40">Next</button></div></div>
    </section>
  </div>
}
