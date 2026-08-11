import { useState, useEffect, useCallback } from 'react'
import { fetchWarmingCustomerBudgets, setWarmingCustomerBudget } from '../lib/api.js'

// Only the providers warming can actually be enabled for. openai is a warming
// provider in the schema but has no keep-alive pipeline, so offering it here
// would offer a ceiling on spend that cannot happen.
//
// deepseek dropped 2026-08-11 as the same kind of dead ceiling, though on
// measurement rather than a missing pipeline. The enrollment endpoint now
// returns 400 on enabling it (api/server.py _WARM_ACTIVE_PROVIDERS is
// anthropic-only; _WARM_INACTIVE_REASONS carries the deepseek reason), because
// DeepSeek's cache is automatic and write-free and a prefix measured STILL
// FULLY WARM at a 900s untouched gap — 1792 hit / 47 miss
// (docs/DEEPSEEK_CACHE_MAP.md P3) — so a keep-alive ping converts no cold read
// into a warm one, and a live n=36 A/B against native caching put warming it at
// -1.27% incremental savings. Note the PUT itself still accepts any provider in
// the schema's _WARM_PROVIDERS, so this list is the only thing keeping an
// operator from budgeting for spend that can never be incurred.
const PROVIDERS = ['anthropic']

const money = (value) => `$${Number(value || 0).toFixed(4)}`

// A tombstoned row is the residue of an erasure: the dollars are the
// organization's own accounting and stay visible, the key does not.
const label = (customerId) => (
  String(customerId || '').startsWith('erased:')
    ? 'erased customer'
    : `${String(customerId || '').slice(0, 8)}…`
)

export default function WarmingBudgets({ accessToken, refreshTick = 0 }) {
  const [rows, setRows] = useState([])
  const [period, setPeriod] = useState('')
  const [error, setError] = useState('')
  // 403 is the expected answer for a member without billing:manage, not a
  // failure worth an error banner — the card just does not exist for them.
  const [visible, setVisible] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ customerId: '', provider: 'anthropic', envelope: '' })

  const load = useCallback(async () => {
    if (!accessToken) return
    try {
      const data = await fetchWarmingCustomerBudgets(accessToken)
      setRows(Array.isArray(data?.budgets) ? data.budgets : [])
      setPeriod(String(data?.period_start || ''))
      setError('')
      setVisible(true)
    } catch (failure) {
      if (failure?.status === 403 || failure?.status === 401) { setVisible(false); return }
      setError(String(failure?.message || 'Could not load warming budgets'))
    }
  }, [accessToken])

  useEffect(() => { load() }, [load, refreshTick])

  const submit = async (event) => {
    event.preventDefault()
    setSaving(true)
    try {
      await setWarmingCustomerBudget(accessToken, {
        provider: form.provider,
        customer_id: form.customerId.trim(),
        envelope_usd: Number(form.envelope),
      })
      setForm({ ...form, customerId: '', envelope: '' })
      setError('')
      await load()
    } catch (failure) {
      setError(String(failure?.message || 'Could not set the envelope'))
    } finally {
      setSaving(false)
    }
  }

  if (!accessToken || !visible) return null

  return (
    <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-4 sm:p-8 overflow-hidden">
      <div className="mb-6">
        <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
          per-customer warming <em className="italic text-brand-blue">envelopes</em>
        </p>
        <p className="text-[11px] text-brand-muted dark:text-brand-dark-muted mt-1">
          {`// ${period || 'current month'} — a customer with no envelope is unconstrained`}
        </p>
      </div>

      {error && (
        <p className="text-[12px] text-red-600 dark:text-red-400 mb-4">{error}</p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-[12px] text-left">
          <thead className="text-brand-muted dark:text-brand-dark-muted">
            <tr>
              <th className="py-2 pr-4 font-normal">customer</th>
              <th className="py-2 pr-4 font-normal">provider</th>
              <th className="py-2 pr-4 font-normal">envelope</th>
              <th className="py-2 pr-4 font-normal">reserved</th>
              <th className="py-2 pr-4 font-normal">spent</th>
              <th className="py-2 pr-4 font-normal">source</th>
            </tr>
          </thead>
          <tbody className="text-brand-navy dark:text-brand-dark-navy">
            {rows.length === 0 && (
              <tr><td className="py-3 text-brand-muted dark:text-brand-dark-muted" colSpan={6}>
                No envelopes this period.
              </td></tr>
            )}
            {rows.map((row) => (
              <tr key={`${row.provider}:${row.customer_id}`} className="border-t border-brand-border dark:border-brand-dark-border">
                <td className="py-2 pr-4 font-mono">{label(row.customer_id)}</td>
                <td className="py-2 pr-4">{row.provider}</td>
                <td className="py-2 pr-4">{money(row.envelope_usd)}</td>
                <td className="py-2 pr-4">{money(row.reserved_usd)}</td>
                <td className="py-2 pr-4">{money(row.spent_usd)}</td>
                <td className="py-2 pr-4">{row.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <form onSubmit={submit} className="mt-6 flex flex-wrap gap-3 items-end">
        <label className="flex flex-col gap-1 text-[11px] text-brand-muted dark:text-brand-dark-muted">
          customer id
          <input
            value={form.customerId}
            onChange={(event) => setForm({ ...form, customerId: event.target.value })}
            placeholder="00000000-0000-0000-0000-000000000000"
            className="font-mono text-[12px] px-2 py-1 rounded border border-brand-border dark:border-brand-dark-border bg-transparent text-brand-navy dark:text-brand-dark-navy"
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-brand-muted dark:text-brand-dark-muted">
          provider
          <select
            value={form.provider}
            onChange={(event) => setForm({ ...form, provider: event.target.value })}
            className="text-[12px] px-2 py-1 rounded border border-brand-border dark:border-brand-dark-border bg-transparent text-brand-navy dark:text-brand-dark-navy"
          >
            {PROVIDERS.map((provider) => (
              <option key={provider} value={provider}>{provider}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-brand-muted dark:text-brand-dark-muted">
          envelope (USD / month)
          <input
            type="number" min="0" max="99999999" step="0.0001"
            value={form.envelope}
            onChange={(event) => setForm({ ...form, envelope: event.target.value })}
            className="text-[12px] px-2 py-1 rounded border border-brand-border dark:border-brand-dark-border bg-transparent text-brand-navy dark:text-brand-dark-navy"
            required
          />
        </label>
        <button
          type="submit"
          disabled={saving}
          className="text-[12px] px-3 py-1.5 rounded bg-brand-blue text-white disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Set envelope'}
        </button>
      </form>
    </div>
  )
}
