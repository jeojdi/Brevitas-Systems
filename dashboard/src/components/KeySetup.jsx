import { useState } from 'react'

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  )
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5"/>
      <line x1="12" y1="1" x2="12" y2="3"/>
      <line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/>
      <line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  )
}

export default function KeySetup({ onSave, darkMode, onToggleDark }) {
  const [name, setName]           = useState('')
  const [pastedKey, setPastedKey] = useState('')
  const [newKey, setNewKey]       = useState('')
  const [loading, setLoading]     = useState(false)
  const [copied, setCopied]       = useState(false)
  const [error, setError]         = useState('')

  const createKey = async () => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/v1/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() || 'default' }),
      })
      if (!res.ok) throw new Error()
      const data = await res.json()
      setNewKey(data.api_key)
      onSave(data.api_key)
    } catch {
      setError('Could not reach the API server. Make sure it is running on port 8000.')
    } finally {
      setLoading(false)
    }
  }

  const copy = () => {
    navigator.clipboard.writeText(newKey)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col items-center justify-center p-6 relative">
      {/* Dark mode toggle */}
      {onToggleDark && (
        <button
          onClick={onToggleDark}
          className="absolute top-6 right-6 text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
          title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {darkMode ? <SunIcon /> : <MoonIcon />}
        </button>
      )}

      {/* Editorial header */}
      <div className="text-center mb-14">
        <div className="flex items-baseline justify-center gap-2 mb-6">
          <span className="font-serif text-5xl font-medium text-brand-navy dark:text-brand-dark-navy">Brevitas</span>
          <span className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">Systems</span>
        </div>
        <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid leading-snug">
          the layer <em className="text-brand-blue not-italic font-serif italic">between</em> your agents.
        </p>
        <p className="annotation mt-3">
          // reduce inter-agent tokens by ~60% · retain 99% of context
        </p>
      </div>

      {/* Card */}
      <div className="w-full max-w-sm bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-8 space-y-6">
        {/* Create section */}
        <div className="space-y-3">
          <p className="annotation">// create a new key</p>
          <input
            type="text"
            placeholder="Project name"
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && createKey()}
            className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
          />
          <button
            onClick={createKey}
            disabled={loading}
            className="w-full bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:opacity-50"
          >
            {loading ? 'Creating…' : 'Create API key →'}
          </button>

          {newKey && (
            <div className="bg-brand-teal-dim dark:bg-brand-dark-teal-dim border border-brand-teal/30 rounded-xl p-4">
              <p className="annotation text-brand-teal mb-2">// copy it now — shown once</p>
              <div className="flex items-center gap-2">
                <code className="flex-1 text-xs font-mono text-brand-teal break-all">{newKey}</code>
                <button
                  onClick={copy}
                  className="shrink-0 border border-brand-teal/40 text-brand-teal rounded-lg px-3 py-1.5 text-xs transition-colors hover:bg-brand-teal hover:text-white"
                >
                  {copied ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Divider */}
        <div className="flex items-center gap-3">
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
          <span className="annotation">or</span>
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        </div>

        {/* Paste section */}
        <div className="space-y-3">
          <p className="annotation">// paste an existing key</p>
          <input
            type="text"
            placeholder="bvt_…"
            value={pastedKey}
            onChange={e => setPastedKey(e.target.value)}
            className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors font-mono"
          />
          <button
            onClick={() => pastedKey.trim() && onSave(pastedKey.trim())}
            disabled={!pastedKey.trim()}
            className="w-full border border-brand-border dark:border-brand-dark-border hover:border-brand-navy dark:hover:border-brand-dark-navy text-brand-navy dark:text-brand-dark-navy rounded-xl px-4 py-3 text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Use this key
          </button>
        </div>

        {error && <p className="font-mono text-xs text-red-500">{error}</p>}
      </div>

      <p className="annotation mt-8 text-center">
        // start server first: <span className="text-brand-navy dark:text-brand-dark-navy">uvicorn api.server:app --reload</span>
      </p>
    </div>
  )
}
