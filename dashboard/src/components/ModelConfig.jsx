import { useState, useEffect, useCallback } from 'react'

const PROVIDERS = [
  {
    id: 'ollama',
    label: 'Ollama',
    description: 'Local models via Ollama — no API key required',
    needsKey: false,
    keyLabel: '',
    keyPlaceholder: '',
    models: [],        // populated dynamically from Ollama
    customModel: true,
  },
  {
    id: 'anthropic',
    label: 'Claude',
    description: 'Anthropic — Claude Opus, Sonnet, Haiku',
    needsKey: true,
    keyLabel: 'Anthropic API key',
    keyPlaceholder: 'sk-ant-…',
    models: ['claude-sonnet-4-6', 'claude-opus-4-8', 'claude-haiku-4-5-20251001'],
    customModel: false,
  },
  {
    id: 'openai',
    label: 'ChatGPT',
    description: 'OpenAI — GPT-4o, o3-mini',
    needsKey: true,
    keyLabel: 'OpenAI API key',
    keyPlaceholder: 'sk-…',
    models: ['gpt-4o', 'gpt-4o-mini', 'o3-mini'],
    customModel: false,
  },
  {
    id: 'grok',
    label: 'Grok',
    description: 'xAI — Grok 3',
    needsKey: true,
    keyLabel: 'xAI API key',
    keyPlaceholder: 'xai-…',
    models: ['grok-3', 'grok-3-mini'],
    customModel: false,
  },
  {
    id: 'deepseek',
    label: 'DeepSeek',
    description: 'DeepSeek — chat & reasoner',
    needsKey: true,
    keyLabel: 'DeepSeek API key',
    keyPlaceholder: 'sk-…',
    models: ['deepseek-chat', 'deepseek-reasoner'],
    customModel: false,
  },
]

export default function ModelConfig({ apiKey }) {
  const [current, setCurrent]           = useState(null)
  const [provider, setProvider]         = useState('ollama')
  const [model, setModel]               = useState('')
  const [customModel, setCustomModel]   = useState('')
  const [providerKey, setProviderKey]   = useState('')
  const [showKey, setShowKey]           = useState(false)
  const [saving, setSaving]             = useState(false)
  const [testing, setTesting]           = useState(false)
  const [status, setStatus]             = useState(null)
  const [loading, setLoading]           = useState(true)
  const [ollamaModels, setOllamaModels] = useState([])

  const providerDef = PROVIDERS.find(p => p.id === provider)
  const displayModels = provider === 'ollama' ? ollamaModels : (providerDef?.models ?? [])

  const loadCurrent = useCallback(async () => {
    setLoading(true)
    try {
      const [providerRes, ollamaRes] = await Promise.all([
        fetch('/v1/provider', { headers: { 'X-API-Key': apiKey } }),
        fetch('/v1/ollama/models', { headers: { 'X-API-Key': apiKey } }),
      ])

      if (ollamaRes.ok) {
        const od = await ollamaRes.json()
        setOllamaModels(od.models ?? [])
        if (providerRes.ok) {
          const data = await providerRes.json()
          setCurrent(data)
          setProvider(data.provider)
          const knownModels = data.provider === 'ollama' ? od.models : (PROVIDERS.find(p => p.id === data.provider)?.models ?? [])
          if (!knownModels.includes(data.model)) {
            setCustomModel(data.model)
            setModel('__custom__')
          } else {
            setModel(data.model)
          }
        } else if (od.models.length > 0) {
          setModel(od.models[0])
        }
      } else if (providerRes.ok) {
        const data = await providerRes.json()
        setCurrent(data)
        setProvider(data.provider)
        setModel(data.model)
      }
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { loadCurrent() }, [loadCurrent])

  // reset model when provider changes
  useEffect(() => {
    const models = provider === 'ollama' ? ollamaModels : (PROVIDERS.find(p => p.id === provider)?.models ?? [])
    setModel(models[0] ?? '')
    setCustomModel('')
    setProviderKey('')
    setStatus(null)
  }, [provider, ollamaModels])

  const resolvedModel = model === '__custom__' ? customModel : model

  const save = async () => {
    setSaving(true); setStatus(null)
    try {
      const res = await fetch('/v1/provider', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({ provider, provider_api_key: providerKey, model: resolvedModel }),
      })
      if (!res.ok) {
        const err = await res.json()
        setStatus({ ok: false, msg: err.detail ?? 'Save failed' })
      } else {
        setStatus({ ok: true, msg: 'Saved.' })
        await loadCurrent()
        setProviderKey('')
      }
    } catch (e) {
      setStatus({ ok: false, msg: e.message })
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    setTesting(true); setStatus(null)
    try {
      const res = await fetch('/v1/compress', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({
          task: 'ping',
          messages: ['hello'],
          prior_context: [],
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      const resp = data.model_response ?? ''
      if (resp.startsWith('[') && resp.includes('error')) {
        setStatus({ ok: false, msg: `Model error: ${resp}` })
      } else {
        setStatus({ ok: true, msg: `Connected. Model replied: "${resp.slice(0, 80)}${resp.length > 80 ? '…' : ''}"` })
      }
    } catch (e) {
      setStatus({ ok: false, msg: e.message })
    } finally {
      setTesting(false)
    }
  }

  if (loading) return <p className="annotation pt-8">// loading…</p>

  return (
    <div className="space-y-14 max-w-2xl">
      {/* Header */}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Model</p>
        <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          Choose your<br />
          <em className="italic text-brand-teal">inference backend.</em>
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-4 leading-relaxed">
          Brevitas compresses your context — the model below receives it.
          Use a local Ollama model or any cloud provider.
        </p>
      </div>

      {/* Current config pill */}
      {current && (
        <div className="flex items-center gap-3 bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3.5">
          <span className="w-2 h-2 rounded-full bg-brand-teal shrink-0" />
          <span className="annotation">
            active &rarr;{' '}
            <span className="text-brand-navy dark:text-brand-dark-navy">
              {PROVIDERS.find(p => p.id === current.provider)?.label ?? current.provider}
            </span>
            {' / '}
            <span className="text-brand-blue font-mono">{current.model}</span>
            {current.has_api_key && (
              <span className="ml-2 text-brand-muted dark:text-brand-dark-muted">· key {current.masked_key}</span>
            )}
          </span>
        </div>
      )}

      {/* Provider grid */}
      <div className="space-y-3">
        <p className="annotation">// select provider</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {PROVIDERS.map(p => (
            <button
              key={p.id}
              onClick={() => setProvider(p.id)}
              className={`text-left rounded-xl border px-4 py-3.5 transition-colors ${
                provider === p.id
                  ? 'border-brand-blue bg-brand-blue-dim dark:bg-brand-dark-blue-dim'
                  : 'border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface hover:border-brand-blue'
              }`}
            >
              <p className={`text-sm font-medium ${provider === p.id ? 'text-brand-blue' : 'text-brand-navy dark:text-brand-dark-navy'}`}>
                {p.label}
              </p>
              <p className="annotation mt-0.5 leading-snug">{p.description}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Model selector */}
      <div className="space-y-3">
        <p className="annotation">// select model</p>
        <div className="flex flex-wrap gap-2">
          {displayModels.length === 0 && provider === 'ollama' && (
            <p className="annotation text-brand-muted dark:text-brand-dark-muted">
              // no models found — run <span className="text-brand-navy dark:text-brand-dark-navy">ollama pull &lt;model&gt;</span> to add one, or use custom below
            </p>
          )}
          {displayModels.map(m => (
            <button
              key={m}
              onClick={() => setModel(m)}
              className={`font-mono text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                model === m
                  ? 'border-brand-blue bg-brand-blue text-white'
                  : 'border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface text-brand-navy dark:text-brand-dark-navy hover:border-brand-blue'
              }`}
            >
              {m}
            </button>
          ))}
          {providerDef?.customModel && (
            <button
              onClick={() => setModel('__custom__')}
              className={`font-mono text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                model === '__custom__'
                  ? 'border-brand-blue bg-brand-blue text-white'
                  : 'border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface text-brand-navy dark:text-brand-dark-navy hover:border-brand-blue'
              }`}
            >
              custom…
            </button>
          )}
        </div>
        {model === '__custom__' && (
          <input
            value={customModel}
            onChange={e => setCustomModel(e.target.value)}
            placeholder="model name (e.g. llama3.1:70b)"
            className="w-full bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm font-mono text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
          />
        )}
      </div>

      {/* API key (non-Ollama) */}
      {providerDef?.needsKey && (
        <div className="space-y-2">
          <p className="annotation">// {providerDef.keyLabel}</p>
          <div className="relative">
            <input
              type={showKey ? 'text' : 'password'}
              value={providerKey}
              onChange={e => setProviderKey(e.target.value)}
              placeholder={current?.has_api_key ? '(key saved — enter to replace)' : providerDef.keyPlaceholder}
              className="w-full bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 pr-16 text-sm font-mono text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
            />
            <button
              onClick={() => setShowKey(v => !v)}
              className="absolute right-4 top-1/2 -translate-y-1/2 annotation hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
            >
              {showKey ? 'hide' : 'show'}
            </button>
          </div>
          <p className="annotation text-brand-muted dark:text-brand-dark-muted">
            // stored locally in SQLite — never sent anywhere except the provider
          </p>
        </div>
      )}

      {/* Status */}
      {status && (
        <p className={`font-mono text-xs ${status.ok ? 'text-brand-teal' : 'text-red-500'}`}>
          {status.ok ? '✓ ' : '✗ '}{status.msg}
        </p>
      )}

      {/* Actions */}
      <div className="flex gap-3">
        <button
          onClick={save}
          disabled={saving || !resolvedModel || (providerDef?.needsKey && !providerKey && !current?.has_api_key)}
          className="flex-1 bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-4 py-3.5 text-sm font-medium transition-colors disabled:opacity-40"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={test}
          disabled={testing}
          className="px-6 rounded-xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface text-brand-navy dark:text-brand-dark-navy text-sm font-medium hover:border-brand-blue transition-colors disabled:opacity-40"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
      </div>
    </div>
  )
}
