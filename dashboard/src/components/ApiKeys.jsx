import { useState, useEffect, useRef } from 'react'
import { apiKeyId, createKey, fetchKeys, revokeKey } from '../lib/api.js'
import { capture } from '../lib/analytics.js'

function CopyButton({ text, small = false }) {
  const [copied, setCopied] = useState(false)
  const copy = () => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000) }
  return (
    <button
      onClick={copy}
      className={`border border-brand-border dark:border-brand-dark-border hover:border-brand-blue text-brand-muted dark:text-brand-dark-muted hover:text-brand-blue rounded-xl transition-colors font-mono ${
        small ? 'px-3 py-1.5 text-xs' : 'px-4 py-2 text-sm'
      }`}
    >
      {copied ? 'copied!' : 'copy'}
    </button>
  )
}

const ENDPOINTS = [
  ['POST', '/v1/compress', 'compress messages + prune context'],
  ['GET',  '/v1/stats',    'usage stats for this key'],
  ['POST', '/v1/keys',     'create a new api key'],
  ['GET',  '/v1/health',   'server health check'],
]

export default function ApiKeys({ apiKey, onApiKeyChange }) {
  const [keys, setKeys]       = useState([])
  const [name, setName]       = useState('')
  const [newKey, setNewKey]   = useState('')
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [error, setError]     = useState('')
  const [activeId, setActiveId] = useState(null)
  const requestId = useRef(0)

  const loadKeys = async (key = apiKey) => {
    const id = ++requestId.current
    setLoading(true)
    setError('')
    try {
      const data = await fetchKeys(key)
      if (id === requestId.current) setKeys(data.keys ?? [])
    } catch (e) {
      if (id === requestId.current) setError(e.message)
    } finally {
      if (id === requestId.current) setLoading(false)
    }
  }

  useEffect(() => { loadKeys() }, [apiKey])
  useEffect(() => {
    let active = true
    setActiveId(null)
    apiKeyId(apiKey).then(id => { if (active) setActiveId(id) }).catch(() => { if (active) setActiveId('') })
    return () => { active = false }
  }, [apiKey])

  const create = async () => {
    if (creating) return
    setCreating(true); setError(''); setNewKey('')
    try {
      const data = await createKey(apiKey, name.trim() || 'unnamed')
      setNewKey(data.api_key)
      setName('')
      capture('api_key_created')
      await onApiKeyChange?.(data.api_key)
      await loadKeys(data.api_key)
    } catch (e) {
      setError(e.message)
    } finally {
      setCreating(false)
    }
  }

  const revoke = async (id) => {
    if (!activeId || id === activeId) return
    if (!window.confirm('Revoke this API key? Calls using it will stop within 30 seconds.')) return
    setError('')
    try {
      await revokeKey(apiKey, id)
      capture('api_key_revoked')
      await loadKeys()
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div className="max-w-2xl space-y-14 ph-no-capture" data-ph-sensitive>
      {/* ── Header ── */}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Key Management</p>
        <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy">
          Your <em className="italic text-brand-blue">API keys.</em>
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-sm mt-3 leading-relaxed">
          Keys are separate credentials; usage from every key you own rolls into this account dashboard.
        </p>
      </div>

      {/* ── Create ── */}
      <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-7 space-y-4">
        <p className="annotation">// create a new key</p>
        <div className="flex flex-col sm:flex-row gap-3">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && create()}
            maxLength={100}
            placeholder="Project name"
            className="flex-1 bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
          />
          <button
            onClick={create}
            disabled={creating}
            className="bg-brand-blue hover:bg-brand-navy disabled:opacity-50 text-white rounded-xl px-5 py-3 text-sm font-medium transition-colors whitespace-nowrap"
          >
            {creating ? 'Creating…' : 'Create →'}
          </button>
        </div>

        {newKey && (
          <div aria-live="polite" className="bg-brand-teal-dim dark:bg-brand-dark-teal-dim border border-brand-teal/30 rounded-xl p-4">
            <p className="annotation text-brand-teal mb-2">// shown once — copy now</p>
            <div className="flex items-center gap-3">
              <code className="flex-1 text-xs font-mono text-brand-teal break-all">{newKey}</code>
              <CopyButton text={newKey} small />
            </div>
          </div>
        )}

        {error && <p role="alert" className="font-mono text-xs text-red-500">{error}</p>}
      </div>

      {/* ── Existing keys ── */}
      <div className="space-y-3">
        <div className="flex items-center gap-4">
          <p className="annotation tracking-widest uppercase shrink-0">Existing Keys</p>
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        </div>

        {loading ? (
          <p className="annotation">// loading keys…</p>
        ) : keys.length === 0 ? (
          <p className="annotation">// no keys yet</p>
        ) : (
          <div className="space-y-2">
            {keys.map((k, i) => (
              <div
                key={k.id || i}
                className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-4 flex flex-wrap items-center justify-between gap-3"
              >
                <div>
                  <p className="text-sm font-medium text-brand-navy dark:text-brand-dark-navy">{k.name}</p>
                  <p className="annotation mt-0.5">
                    // created {new Date(k.created).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })}
                  </p>
                </div>
                <button
                  onClick={() => revoke(k.id)}
                  disabled={!activeId || k.id === activeId}
                  className="font-mono text-[10px] uppercase tracking-widest text-red-500 hover:underline disabled:text-brand-muted disabled:no-underline"
                >
                  {k.id === activeId ? 'Active' : 'Revoke'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Active session key ── */}
      <div className="space-y-3">
        <div className="flex items-center gap-4">
          <p className="annotation tracking-widest uppercase shrink-0">Active Key</p>
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        </div>
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-4 flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
          <code className="flex-1 text-xs font-mono text-brand-muted dark:text-brand-dark-muted truncate">{apiKey}</code>
          <CopyButton text={apiKey} small />
        </div>
      </div>

      {/* ── API Reference ── */}
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <p className="annotation tracking-widest uppercase shrink-0">API Reference</p>
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        </div>
        <div className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border p-6 space-y-3">
          {ENDPOINTS.map(([method, path, desc]) => (
            <div key={path} className="flex items-start gap-4">
              <span
                className={`shrink-0 font-mono text-[10px] tracking-wide px-2 py-1 rounded-lg font-medium ${
                  method === 'POST'
                    ? 'bg-brand-blue-dim dark:bg-brand-dark-blue-dim text-brand-blue'
                    : 'bg-brand-teal-dim dark:bg-brand-dark-teal-dim text-brand-teal'
                }`}
              >
                {method}
              </span>
              <div className="min-w-0">
                <p className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy break-all">{path}</p>
                <p className="annotation mt-0.5">// {desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
