import { useState, useEffect, useCallback } from 'react'
import {
  compress, fetchOllamaModels, fetchProvider, fetchProviders, saveProvider,
} from '../lib/api.js'

const PROVIDER_META = {
  ollama:    ['Ollama', 'Ollama on the Brevitas server — no API key required', ''],
  anthropic: ['Claude', 'Anthropic Claude models', 'sk-ant-…'],
  openai:    ['OpenAI', 'OpenAI models', 'sk-…'],
  grok:      ['Grok', 'xAI Grok models', 'xai-…'],
  deepseek:  ['DeepSeek', 'DeepSeek chat and reasoning models', 'sk-…'],
}

function providerDefinition(id, models) {
  const meta = PROVIDER_META[id] || [id.replaceAll('_', ' '), `${id.replaceAll('_', ' ')} models`, 'API key']
  return {
    id, models, label: meta[0], description: meta[1], keyPlaceholder: meta[2],
    needsKey: id !== 'ollama',
  }
}

export default function ModelConfig({ apiKey }) {
  const [current, setCurrent]           = useState(null)
  const [provider, setProvider]         = useState('ollama')
  const [model, setModel]               = useState('')
  const [providerKey, setProviderKey]   = useState('')
  const [showKey, setShowKey]           = useState(false)
  const [saving, setSaving]             = useState(false)
  const [testing, setTesting]           = useState(false)
  const [status, setStatus]             = useState(null)
  const [loading, setLoading]           = useState(true)
  const [ollamaModels, setOllamaModels] = useState([])
  const [ollamaAvailable, setOllamaAvailable] = useState(false)
  const [catalog, setCatalog]           = useState({})

  const providers = Object.entries({ ...catalog, ollama: ollamaModels })
    .filter(([, models]) => models.length > 0)
    .map(([id, models]) => providerDefinition(id, models))
  const providerDef = providers.find(p => p.id === provider)
  const displayModels = providerDef?.models ?? []
  const savedKeyForProvider = current?.provider === provider && current?.has_api_key
  const editingActive = current?.configured && current.provider === provider && current.model === model

  const loadCurrent = useCallback(async () => {
    setLoading(true)
    setStatus(null)
    try {
      const [data, providerCatalog, ollama] = await Promise.all([
        fetchProvider(apiKey),
        fetchProviders(apiKey),
        fetchOllamaModels(apiKey).catch(() => ({ models: [] })),
      ])
      const models = data.provider === 'ollama'
        ? (ollama.available ? ollama.models ?? [] : [])
        : (providerCatalog.providers?.[data.provider] ?? [])
      setCatalog(providerCatalog.providers ?? {})
      setOllamaAvailable(Boolean(ollama.available))
      setOllamaModels(ollama.available ? ollama.models ?? [] : [])
      setCurrent(data)
      setProvider(data.provider)
      setProviderKey('')
      if (models.includes(data.model)) {
        setModel(data.model)
      } else {
        setModel('')
        if (data.provider === 'ollama' && !ollama.available) {
          setStatus({ ok: false, msg: 'Ollama is not available on the Brevitas server. Choose a cloud provider.' })
        }
      }
    } catch (error) {
      setStatus({ ok: false, msg: error.message })
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { loadCurrent() }, [loadCurrent])

  const selectProvider = id => {
    const models = id === 'ollama' ? ollamaModels : (catalog[id] ?? [])
    setProvider(id)
    setModel(models[0] ?? '')
    setProviderKey('')
    setStatus(null)
  }

  const resolvedModel = model

  const save = async () => {
    setSaving(true); setStatus(null)
    try {
      await saveProvider(apiKey, { provider, provider_api_key: providerKey, model: resolvedModel })
      await loadCurrent()
      setStatus({ ok: true, msg: 'Saved.' })
    } catch (e) {
      setStatus({ ok: false, msg: e.message })
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    setTesting(true); setStatus(null)
    try {
      const data = await compress(apiKey, {
        task: 'Reply with OK.', messages: ['Reply with OK.'], prior_context: [], meter: false,
      })
      const resp = data.model_response ?? ''
      if (!resp || (resp.startsWith('[') && resp.includes('error'))) {
        setStatus({ ok: false, msg: `Model error: ${resp}` })
      } else {
        setStatus({ ok: true, msg: `Connected to ${data.provider || current?.provider} / ${data.model || current?.model}.` })
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
          Choose one of the providers and models currently supported by the API.
        </p>
      </div>

      {/* Current config pill */}
      {current?.configured && (
        <div className="flex items-center gap-3 bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3.5">
          <span className="w-2 h-2 rounded-full bg-brand-teal shrink-0" />
          <span className="annotation">
            active &rarr;{' '}
            <span className="text-brand-navy dark:text-brand-dark-navy">
              {providers.find(p => p.id === current.provider)?.label ?? current.provider}
            </span>
            {' / '}
            <span className="text-brand-blue font-mono">{current.model}</span>
            {current.has_api_key && (
              <span className="ml-2 text-brand-muted dark:text-brand-dark-muted">· key {current.masked_key}</span>
            )}
          </span>
        </div>
      )}
      {current && !current.configured && (
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3.5">
          <span className="annotation">// no model provider configured</span>
        </div>
      )}

      {/* Provider grid */}
      <div className="space-y-3">
        <p className="annotation">// select provider</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {providers.map(p => (
            <button
              key={p.id}
              onClick={() => selectProvider(p.id)}
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
              // Ollama is unavailable on the Brevitas server — choose a cloud provider
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
        </div>
      </div>

      {/* API key (non-Ollama) */}
      {providerDef?.needsKey && (
        <div className="space-y-2">
          <p className="annotation">// {providerDef.label} API key</p>
          <div className="relative">
            <input
              type={showKey ? 'text' : 'password'}
              value={providerKey}
              onChange={e => setProviderKey(e.target.value)}
              placeholder={savedKeyForProvider ? '(key saved — enter to replace)' : providerDef.keyPlaceholder}
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
            // encrypted at rest — used only for requests to the selected provider
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
      <div className="flex flex-col sm:flex-row gap-3">
        <button
          onClick={save}
          disabled={saving || !resolvedModel || (providerDef?.needsKey && !providerKey && !savedKeyForProvider)}
          className="flex-1 bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-4 py-3.5 text-sm font-medium transition-colors disabled:opacity-40"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={test}
          disabled={testing || saving || !editingActive || (current.provider === 'ollama' && !ollamaAvailable)}
          className="px-6 py-3.5 rounded-xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface text-brand-navy dark:text-brand-dark-navy text-sm font-medium hover:border-brand-blue transition-colors disabled:opacity-40"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
      </div>
    </div>
  )
}
