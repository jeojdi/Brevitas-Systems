import { useState } from 'react'

export default function DeviceConnect({ accessToken, deviceCode, email, onDone }) {
  const [status, setStatus] = useState('ready')
  const [error, setError] = useState('')

  const approve = async () => {
    setStatus('loading')
    setError('')
    try {
      const response = await fetch('/v1/device-auth/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ device_code: deviceCode }),
      })
      if (!response.ok) throw new Error((await response.json()).detail || 'Connection failed')
      setStatus('done')
    } catch (err) {
      setError(err.message)
      setStatus('ready')
    }
  }

  if (status === 'done') return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center px-4">
      <div className="w-full max-w-md bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-8 text-center">
        <div className="w-10 h-10 rounded-full bg-brand-teal-dim text-brand-teal mx-auto mb-5 flex items-center justify-center text-xl">✓</div>
        <h1 className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">bvx is connected.</h1>
        <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-3">Return to your terminal. Installation will continue automatically.</p>
        <button onClick={onDone} className="mt-7 font-mono text-[11px] tracking-widest uppercase text-brand-blue">Open dashboard</button>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center px-4">
      <div className="w-full max-w-md bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-8">
        <p className="annotation tracking-widest uppercase mb-4">Connect device</p>
        <h1 className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">Allow bvx on this computer?</h1>
        <p className="text-sm text-brand-muted dark:text-brand-dark-muted mt-3 leading-relaxed">
          This creates a revocable API key for <span className="text-brand-navy dark:text-brand-dark-navy">{email}</span> and stores it in your operating system credential manager.
        </p>
        <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted mt-4">No provider key, prompt, response, code, or file path is shared.</p>
        {error && <p className="font-mono text-xs text-red-500 mt-4">{error}</p>}
        <div className="flex gap-3 mt-7">
          <button onClick={approve} disabled={status === 'loading'} className="flex-1 bg-brand-blue text-white rounded-xl px-5 py-3 text-sm font-medium disabled:opacity-50">
            {status === 'loading' ? 'Connecting…' : 'Approve connection'}
          </button>
          <button onClick={onDone} className="border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3 text-sm text-brand-muted">Cancel</button>
        </div>
      </div>
    </div>
  )
}
