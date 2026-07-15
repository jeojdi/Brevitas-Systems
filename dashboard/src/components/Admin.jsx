import { useEffect, useState } from 'react'

const num = value => Number(value || 0).toLocaleString()
const usd = value => `$${Number(value || 0).toFixed(4)}`
const measured = row => row.unpriced_calls === row.calls ? 'Unpriced' : usd(row.measured_savings_usd)

export default function Admin({ accessToken, refreshTick }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    fetch('/v1/admin/stats/breakdown', { headers: { Authorization: `Bearer ${accessToken}` } })
      .then(response => response.ok ? response.json() : Promise.reject(new Error('Admin access denied')))
      .then(setData).catch(error => setError(error.message))
  }, [accessToken, refreshTick])
  if (error) return <p className="font-mono text-xs text-red-500">{error}</p>
  if (!data) return <p className="annotation">// loading admin usage…</p>
  return <div className="space-y-8">
    <div><p className="annotation tracking-widest uppercase">Brevitas operations</p><h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">Customer usage.</h2><p className="text-brand-muted mt-3">Numeric receipts only. No prompts, responses, code, paths, remotes, or provider keys.</p></div>
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">{[
      ['Calls', num(data.totals.total_calls)], ['Tokens saved', num(data.totals.total_tokens_saved)],
      ['Measured savings', data.totals.total_calls > 0 && data.totals.unpriced_calls === data.totals.total_calls ? 'Unpriced' : usd(data.totals.total_measured_savings_usd)],
      ['Verified savings', usd(data.totals.total_verified_savings_usd)],
    ].map(([label, value]) => <div key={label} className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6"><p className="annotation">{label}</p><p className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy mt-2">{value}</p></div>)}</div>
    <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface"><table className="w-full min-w-[760px] text-left"><thead><tr>{['Account', 'Project / source', 'Provider / model', 'Calls', 'Tokens saved', 'Measured', 'Verified'].map(label => <th key={label} className="annotation px-4 py-3 border-b border-brand-border dark:border-brand-dark-border">{label}</th>)}</tr></thead><tbody>{data.rows.map((row, index) => <tr key={`${row.account_id}-${row.project}-${index}`} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border"><td className="font-mono text-xs px-4 py-3">{row.account_id}</td><td className="font-mono text-xs px-4 py-3">{row.project} / {row.source}</td><td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.provider} / {row.model}</td><td className="font-mono text-xs px-4 py-3">{num(row.calls)}</td><td className="font-mono text-xs px-4 py-3">{num(row.tokens_saved)}</td><td className="font-mono text-xs px-4 py-3">{measured(row)}</td><td className="font-mono text-xs px-4 py-3 text-brand-teal">{usd(row.verified_savings_usd)}</td></tr>)}</tbody></table></div>
  </div>
}
