import { useEffect, useMemo, useState } from 'react'

const number = n => Number(n || 0).toLocaleString()
const usd = n => n == null ? 'Unpriced' : `$${Number(n).toFixed(4)}`

export default function Projects({ apiKey, refreshTick }) {
  const [rows, setRows] = useState([])
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    fetch('/v1/stats/breakdown', { headers: { 'X-Brevitas-Key': apiKey } })
      .then(async response => {
        if (response.ok) return response.json()
        const error = await response.json().catch(() => ({}))
        throw new Error(error.detail || `Failed to load repositories (${response.status})`)
      })
      .then(data => setRows(data.rows || []))
      .catch(error => setError(error.message))
  }, [apiKey, refreshTick])

  const projects = useMemo(() => Object.values(rows.reduce((all, row) => {
    const name = row.repo || row.project || 'Unattributed'
    const project = all[name] ||= { name, calls: 0, tokens: 0, measured: 0, verified: 0, unpriced: 0, rows: [] }
    project.calls += Number(row.calls || 0)
    project.tokens += Number(row.tokens_saved || 0)
    project.measured += Number(row.measured_savings_usd || 0)
    project.verified += Number(row.verified_savings_usd || 0)
    project.unpriced += Number(row.unpriced_calls || 0)
    project.rows.push(row)
    return all
  }, {})), [rows])

  if (error) return <p className="font-mono text-xs text-red-500 pt-8">{error}</p>
  if (!rows.length) return <div className="pt-16 text-center"><p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">No repository usage yet.</p><p className="annotation mt-2">// set BREVITAS_REPO and make an AI call</p></div>

  const current = projects.find(project => project.name === selected)
  if (current) return (
    <div className="space-y-8">
      <button onClick={() => setSelected('')} className="annotation hover:text-brand-blue">← all repositories</button>
      <div><p className="annotation tracking-widest uppercase">Repository</p><h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">{current.name}</h2></div>
      <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
        <table className="w-full text-left">
          <thead><tr className="border-b border-brand-border dark:border-brand-dark-border">{['Client', 'Provider / model', 'Operation', 'Calls', 'Tokens saved', 'Measured', 'Verified'].map(label => <th key={label} className="annotation px-4 py-3">{label}</th>)}</tr></thead>
          <tbody>{current.rows.map((row, index) => <tr key={`${row.client}-${row.provider}-${row.model}-${index}`} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
            <td className="font-mono text-xs px-4 py-3 text-brand-navy dark:text-brand-dark-navy">{row.client || row.source || 'Unattributed'}{row.environment ? ` / ${row.environment}` : ''}{row.agent ? ` / ${row.agent}` : ''}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.gateway ? `${row.gateway} → ` : ''}{row.provider || 'unknown'} / {row.model || 'unknown'}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-muted">{row.operation}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.calls)}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.tokens_saved)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.unpriced_calls === row.calls ? 'Unpriced' : usd(row.measured_savings_usd)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-teal">{usd(row.verified_savings_usd)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>
  )

  return <div className="space-y-8">
    <div><p className="annotation tracking-widest uppercase">Repositories</p><h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">Every codebase and agent.</h2><p className="text-brand-muted mt-3">Runtime usage discovered through AgentMap integrations.</p></div>
    <div className="grid md:grid-cols-2 gap-4">{projects.map(project => <button key={project.name} onClick={() => setSelected(project.name)} className="text-left bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border hover:border-brand-blue rounded-2xl p-6 transition-colors">
      <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">{project.name}</p>
      <p className="annotation mt-2">{number(project.calls)} calls · {number(project.tokens)} tokens saved</p>
      <div className="flex gap-6 mt-5"><div><p className="annotation">Measured</p><p className="font-mono text-brand-blue">{project.unpriced === project.calls ? 'Unpriced' : usd(project.measured)}</p></div><div><p className="annotation">Verified</p><p className="font-mono text-brand-teal">{usd(project.verified)}</p></div></div>
    </button>)}</div>
  </div>
}
