import { useState } from 'react'
import { capture } from '../lib/analytics.js'
import { redactBrowserError } from '../lib/api.js'

export default function DeviceConnect({
  accessToken,
  deviceCode,
  email,
  companies = [],
  selectedCompanyId = '',
  companyLoading,
  companyError,
  onSelectCompany,
  onRefreshCompanies,
  onDone,
}) {
  const [status, setStatus] = useState('ready')
  const [error, setError] = useState('')

  const approve = async () => {
    const selected = companies.find(company => company.company_id === selectedCompanyId)
    if (!selected) {
      setError('Select an active company for this device.')
      return
    }
    setStatus('loading')
    setError('')
    try {
      const response = await fetch('/v1/device-auth/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ device_code: deviceCode, company_id: selected.company_id }),
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        if (response.status === 409) {
          const selectionRequired = payload.detail === 'Select a company for this device'
          throw new Error(selectionRequired
            ? 'Select a company for this device.'
            : 'This device connection was already handled.')
        }
        if (response.status === 403) throw new Error('Company access denied. Refresh your company access and try again.')
        if (response.status === 401) throw new Error('Sign in again to approve this device.')
        throw new Error(redactBrowserError(payload.detail) || 'Connection failed')
      }
      capture('device_connected')
      setStatus('done')
    } catch (err) {
      setError(redactBrowserError(err?.message) || 'Connection failed')
      setStatus('ready')
    }
  }

  if (status === 'done') return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center px-4 ph-no-capture" data-ph-sensitive>
      <div className="w-full max-w-md bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 sm:p-8 text-center">
        <div className="w-10 h-10 rounded-full bg-brand-teal-dim text-brand-teal mx-auto mb-5 flex items-center justify-center text-xl">✓</div>
        <h1 className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">bvx is connected.</h1>
        <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-3">Return to your terminal. Installation will continue automatically.</p>
        <button onClick={onDone} className="mt-7 font-mono text-[11px] tracking-widest uppercase text-brand-blue">Open dashboard</button>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center px-4 ph-no-capture" data-ph-sensitive>
      <div className="w-full max-w-md bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 sm:p-8">
        <p className="annotation tracking-widest uppercase mb-4">Connect device</p>
        <h1 className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">Allow bvx on this computer?</h1>
        <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-3 leading-relaxed">
          This creates a revocable API key for <span className="text-brand-navy dark:text-brand-dark-navy">{email}</span> and stores it in your operating system credential manager.
        </p>
        <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted mt-4">No provider key, prompt, response, code, or file path is shared.</p>
        <div className="mt-5">
          <p className="annotation block mb-2">Company for this device</p>
          {companies.length > 1 ? <select
            id="device-company"
            aria-label="Company for this device"
            value={selectedCompanyId}
            onChange={event => onSelectCompany(event.target.value)}
            disabled={companyLoading || status === 'loading'}
            className="w-full rounded-xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface px-3 py-2.5 text-sm"
          >
            <option value="" disabled>Select a company</option>
            {companies.map(company => <option key={company.company_id} value={company.company_id}>
              {company.company_name} · {company.role.replaceAll('_', ' ')}
            </option>)}
          </select> : <div id="device-company" className="rounded-xl border border-brand-border dark:border-brand-dark-border px-3 py-2.5 text-sm">
            {companyLoading ? 'Loading company access…' : companies[0]?.company_name || 'Company access unavailable'}
          </div>}
          {companyError && <div className="mt-2 flex items-center justify-between gap-3">
            <p role="alert" className="font-mono text-xs text-red-500">Company access unavailable.</p>
            <button type="button" onClick={onRefreshCompanies} className="text-xs text-brand-blue">Retry</button>
          </div>}
        </div>
        {error && <p className="font-mono text-xs text-red-500 mt-4">{error}</p>}
        <div className="flex flex-col sm:flex-row gap-3 mt-7">
          <button onClick={approve} disabled={status === 'loading' || companyLoading || !selectedCompanyId} className="flex-1 bg-brand-blue text-white rounded-xl px-5 py-3 text-sm font-medium disabled:opacity-50">
            {status === 'loading' ? 'Connecting…' : 'Approve connection'}
          </button>
          <button onClick={onDone} className="border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3 text-sm text-brand-muted">Cancel</button>
        </div>
      </div>
    </div>
  )
}
