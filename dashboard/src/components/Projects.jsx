import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchBreakdown } from '../lib/api.js'

const number = n => Number(n || 0).toLocaleString()
const usd = n => n == null ? 'Unpriced' : `$${Number(n).toFixed(4)}`

export default function Projects({ apiKey, refreshTick }) {
  const [rows, setRows] = useState([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const controllerRef = useRef(null)

  const load = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setError('')
    try {
      const data = await fetchBreakdown(apiKey, { signal: controller.signal })
      if (controllerRef.current === controller) setRows(data.rows || [])
    } catch (error) {
      if (controllerRef.current === controller && error.name !== 'AbortError') setError(error.message)
    } finally {
      if (controllerRef.current === controller) setLoading(false)
    }
  }, [apiKey])

  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load, refreshTick])

  const projects = useMemo(() => Object.values(rows.reduce((all, row) => {
    const name = row.repo || row.project || 'Unattributed'
    const project = all[name] ||= { name, calls: 0, inputAvoided: 0, callsAvoided: 0, spend: 0, verified: 0, unpriced: 0, rows: [] }
    project.calls += Number(row.calls || 0)
    project.inputAvoided += Number(row.provider_input_tokens_avoided || 0)
    project.callsAvoided += Number(row.calls_avoided || 0)
    project.spend += Number(row.actual_cost_usd || 0)
    project.verified += Number(row.verified_savings_usd || 0)
    project.unpriced += Number(row.unpriced_calls || 0)
    project.rows.push(row)
    return all
  }, {})), [rows])

  if (loading) return <p className="annotation pt-8">// loading repositories…</p>
  if (error && !rows.length) return <div className="pt-8"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation mt-3 hover:text-brand-blue">retry</button></div>
  if (!rows.length) return <div className="pt-16 text-center"><p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">No repository usage yet.</p><p className="annotation mt-2">// set BREVITAS_REPO and make an AI call</p></div>

  const current = projects.find(project => project.name === selected)
  if (current) return (
    <div className="space-y-8">
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
      <button onClick={() => setSelected('')} className="annotation hover:text-brand-blue">← all repositories</button>
      <div><p className="annotation tracking-widest uppercase">Repository</p><h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">{current.name}</h2></div>
      <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
        <table className="w-full min-w-[760px] text-left">
          <thead><tr className="border-b border-brand-border dark:border-brand-dark-border">{['Client', 'Provider / model', 'Operation', 'Calls', 'Input avoided', 'Calls avoided', 'Provider spend'].map(label => <th key={label} className="annotation px-4 py-3">{label}</th>)}</tr></thead>
          <tbody>{current.rows.map((row, index) => <tr key={`${row.client}-${row.provider}-${row.model}-${index}`} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
            <td className="font-mono text-xs px-4 py-3 text-brand-navy dark:text-brand-dark-navy">{row.client || row.source || 'Unattributed'}{row.environment ? ` / ${row.environment}` : ''}{row.agent ? ` / ${row.agent}` : ''}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.gateway ? `${row.gateway} → ` : ''}{row.provider || 'unknown'} / {row.model || 'unknown'}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-muted">{row.operation}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.calls)}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.provider_input_tokens_avoided)}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.calls_avoided)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-navy-mid dark:text-brand-dark-navy-mid">{row.unpriced_calls === row.calls ? 'Unpriced' : usd(row.actual_cost_usd)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>
  )

  return <div className="space-y-8">
    {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
    <div><p className="annotation tracking-widest uppercase">Repositories</p><h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">Every codebase and agent.</h2><p className="text-brand-muted mt-3">Runtime usage discovered through AgentMap integrations.</p></div>
    <div className="grid md:grid-cols-2 gap-4">{projects.map(project => <button key={project.name} onClick={() => setSelected(project.name)} className="text-left bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border hover:border-brand-blue rounded-2xl p-6 transition-colors">
      <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">{project.name}</p>
      <p className="annotation mt-2">{number(project.calls)} calls · {number(project.inputAvoided)} provider input tokens avoided · {number(project.callsAvoided)} calls avoided</p>
      <div className="flex flex-wrap gap-6 mt-5"><div><p className="annotation">Provider spend</p><p className="font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid">{project.unpriced === project.calls ? 'Unpriced' : usd(project.spend)}</p></div><div><p className="annotation">Verified savings</p><p className="font-mono text-brand-teal">{usd(project.verified)}</p></div></div>
    </button>)}</div>
  </div>
}
