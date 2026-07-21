import { useState, useRef, useEffect } from 'react'
import { streamPlaygroundChat, fetchProviders } from '../lib/api.js'
import { capture } from '../lib/analytics.js'

// Free zero-config default served by the Brevitas server (no key needed).
const FREE_LABEL = 'hosted gemma2-9b-it · no key needed'

const PROVIDER_LABELS = {
  anthropic: 'Claude', openai: 'OpenAI', grok: 'Grok', groq: 'Groq',
  deepseek: 'DeepSeek', ollama: 'Ollama',
}
const providerLabel = id => PROVIDER_LABELS[id] || id.replaceAll('_', ' ')

const SUGGESTIONS = [
  'Design a Redis-backed rate limiter for our API gateway.',
  'Now add per-user quotas on top of that.',
  'What failure modes should we test for?',
]

// Compact dollar formatting: cents at 2dp, sub-cent at 4dp, tiny values as a floor.
const fmtUsd = (v) => {
  if (!v) return '$0.00'
  if (v < 0.0001) return '<$0.0001'
  if (v < 0.01) return `$${v.toFixed(4)}`
  return `$${v.toFixed(2)}`
}

function TokenBar({ baseline, optimized }) {
  const pct = baseline > 0 ? Math.max(4, Math.round((optimized / baseline) * 100)) : 100
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between">
        <span className="font-mono text-[11px] text-brand-muted dark:text-brand-dark-muted">{baseline} tok baseline</span>
        <span className="font-mono text-[11px] text-brand-blue font-medium">{optimized} tok sent</span>
      </div>
      <div className="h-1.5 bg-brand-border dark:bg-brand-dark-border rounded-full overflow-hidden">
        <div className="h-full bg-brand-blue rounded-full transition-all duration-700" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// Live / final mechanism-separated usage strip attached to a turn.
function SavingsStrip({ meta, live = false }) {
  const retained = meta.retained != null ? `${meta.retained.toFixed(1)}%` : 'not measured'
  const inputAvoided = Math.max(0, Number(meta.baseline || 0) - Number(meta.optimized || 0))
  return (
    <div className={`rounded-xl border p-3.5 space-y-3 ${live
      ? 'border-brand-blue/40 bg-brand-blue-dim dark:bg-brand-dark-blue-dim'
      : 'border-brand-border dark:border-brand-dark-border bg-brand-bg dark:bg-brand-dark-bg'}`}>
      <div className="flex items-center justify-between gap-2">
        <p className="annotation">{live ? '// compressing context…' : '// context compressed'}</p>
        {meta.total > 0 && (
          <p className="annotation">kept {meta.selected}/{meta.total} chunks</p>
        )}
      </div>

      {/* Cache-hit banner — the model call was skipped entirely */}
      {meta.cacheHit && (
        <div className="flex items-center gap-2 rounded-lg bg-brand-teal-dim dark:bg-brand-dark-teal-dim px-3 py-2">
          <span className="text-brand-teal">⚡</span>
          <p className="font-mono text-xs text-brand-teal">
            served from cache · {meta.cacheKind || 'exact'} match · model call skipped
          </p>
        </div>
      )}

      <TokenBar baseline={meta.baseline} optimized={meta.optimized} />
      <div className="grid grid-cols-3 gap-3 pt-0.5">
        <div>
          <p className="font-mono text-2xl font-medium text-brand-blue tabular-nums">
            {meta.cacheHit ? '1' : inputAvoided.toLocaleString()}
          </p>
          <p className="annotation mt-0.5">{meta.cacheHit ? '// model call avoided' : '// provider input tokens avoided'}</p>
        </div>
        <div>
          <p className="font-mono text-2xl font-medium text-brand-teal tabular-nums">{retained}</p>
          <p className="annotation mt-0.5">// measured similarity</p>
        </div>
        <div>
          <p className="font-mono text-2xl font-medium text-brand-navy dark:text-brand-dark-navy tabular-nums">
            {meta.costSaved != null ? fmtUsd(meta.costSaved) : '—'}
          </p>
          <p className="annotation mt-0.5">// estimated cost delta · {meta.priceBasis || 'gpt-4o'}</p>
        </div>
      </div>
    </div>
  )
}

export default function Playground({ apiKey }) {
  const [mode, setMode]           = useState('free')          // 'free' | 'byok'
  const [catalog, setCatalog]     = useState({})
  const [byokProvider, setByokProvider] = useState('')
  const [byokModel, setByokModel] = useState('')
  const [byokKey, setByokKey]     = useState('')              // ephemeral — kept in memory only
  const [showKey, setShowKey]     = useState(false)

  const [turns, setTurns]         = useState([])              // {role, content, meta?}
  const [pending, setPending]     = useState(null)            // live compressed metrics for in-flight turn
  const [input, setInput]         = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError]         = useState('')

  const abortRef = useRef(null)
  const scrollRef = useRef(null)

  useEffect(() => () => abortRef.current?.abort(), [])
  useEffect(() => {
    // Providers that require a key and have selectable models (for BYOK mode).
    fetchProviders(apiKey)
      .then(data => setCatalog(data.providers ?? {}))
      .catch(() => setCatalog({}))
  }, [apiKey])
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, pending])

  const byokProviders = Object.entries(catalog)
    .filter(([id, models]) => models.length > 0 && id !== 'ollama')
    .map(([id]) => id)
  const byokModels = catalog[byokProvider] ?? []

  const selectByokProvider = id => {
    setByokProvider(id)
    setByokModel((catalog[id] ?? [])[0] ?? '')
  }

  // Cumulative session usage effects across all completed turns.
  const totals = turns.reduce((acc, t) => {
    if (t.meta) {
      acc.saved += t.meta.tokensSaved ?? (t.meta.baseline - t.meta.optimized)
      acc.cost  += t.meta.costSaved ?? 0
      acc.hits  += t.meta.callsAvoided ?? (t.meta.cacheHit ? 1 : 0)
      acc.n     += 1
    }
    return acc
  }, { saved: 0, cost: 0, hits: 0, n: 0 })

  const byokReady = mode === 'byok' && byokProvider && byokModel && byokKey
  const canSend = !streaming && input.trim() && (mode === 'free' || byokReady)

  const send = async (text) => {
    const message = (text ?? input).trim()
    if (!message || streaming) return
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller

    const priorContext = turns.map(t => t.content)   // whole conversation so far → gets pruned
    capture('playground_message_sent', { mode, byok_provider: byokReady ? byokProvider : null, turn_index: turns.filter(t => t.role === 'user').length })
    setTurns(prev => [...prev, { role: 'user', content: message }])
    setInput('')
    setError('')
    setPending(null)
    setStreaming(true)

    const body = {
      messages: [message],
      prior_context: priorContext,
      task: message,
      prune_budget: 5,
      ...(byokReady ? { byok_provider: byokProvider, byok_model: byokModel, byok_key: byokKey } : {}),
    }

    let replyText = ''
    let lastMeta = null
    try {
      await streamPlaygroundChat(apiKey, body, (event) => {
        if (event.stage === 'compressed') {
          lastMeta = {
            baseline: event.baseline_tokens,
            optimized: event.optimized_tokens,
            savingsPct: event.savings_pct,
            selected: event.selected,
            total: priorContext.length,
            retained: event.quality_sim != null ? event.quality_sim * 100 : null,
            method: event.method,
          }
          setPending(lastMeta)
        } else if (event.stage === 'cached') {
          // Cache served this turn — flag it live so the badge appears immediately.
          lastMeta = { ...(lastMeta || {}), cacheHit: true, cacheKind: event.kind, cacheSimilarity: event.similarity }
          capture('playground_cache_hit', { cache_kind: event.kind, similarity: event.similarity })
          setPending(lastMeta)
        } else if (event.stage === 'model_response') {
          replyText = event.text || ''
        } else if (event.stage === 'done') {
          const r = event.result || {}
          replyText = replyText || r.model_response || ''
          const meta = {
            ...(lastMeta || {}),
            provider: r.provider, model: r.model,
            cacheHit: r.cache_hit ?? lastMeta?.cacheHit ?? false,
            cacheKind: r.cache_kind || lastMeta?.cacheKind || '',
            tokensSaved: r.provider_input_tokens_avoided
              ?? r.tokens_saved_total
              ?? (lastMeta ? lastMeta.baseline - lastMeta.optimized : 0),
            callsAvoided: r.calls_avoided ?? (r.cache_hit ? 1 : 0),
            costSaved: r.estimated_cost_delta_usd ?? r.cost_saved_usd ?? 0,
            priceBasis: r.price_basis || 'gpt-4o-mini',
          }
          setTurns(prev => [...prev, {
            role: 'assistant',
            content: replyText || '// no model configured — compression-only (add a key to get a reply)',
            empty: !replyText,
            meta,
          }])
          setPending(null)
        }
      }, { signal: controller.signal })
    } catch (e) {
      if (e.name !== 'AbortError') {
        setError(e.message)
        setPending(null)
      }
    } finally {
      setStreaming(false)
    }
  }

  const reset = () => { abortRef.current?.abort(); setTurns([]); setPending(null); setError(''); setStreaming(false) }

  return (
    <div className="space-y-10 ph-no-capture" data-ph-sensitive>
      {/* ── Header ── */}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Playground</p>
        <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          Chat with an agent.<br />
          <em className="italic text-brand-teal">See what happens to every request.</em>
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-4 max-w-xl leading-relaxed">
          Each turn is labeled honestly: unchanged, input-reduced by an explicitly enabled quality-affecting
          lever, or served from response cache without a model call.
        </p>
      </div>

      {/* ── Backend selector ── */}
      <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <p className="annotation mr-1">// model backend</p>
          {[['free', 'Free default'], ['byok', 'Bring your own key']].map(([id, label]) => (
            <button
              key={id}
              onClick={() => { setMode(id); capture('playground_mode_changed', { mode: id }) }}
              className={`font-mono text-xs px-3 py-1.5 rounded-lg border transition-colors ${
                mode === id
                  ? 'border-brand-blue bg-brand-blue text-white'
                  : 'border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface text-brand-navy dark:text-brand-dark-navy hover:border-brand-blue'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {mode === 'free' && (
          <p className="annotation text-brand-muted dark:text-brand-dark-muted">// {FREE_LABEL}</p>
        )}

        {mode === 'byok' && (
          <div className="space-y-4">
            <div className="grid sm:grid-cols-2 gap-3">
              <div>
                <p className="annotation mb-1.5">// provider</p>
                <select
                  value={byokProvider}
                  onChange={e => selectByokProvider(e.target.value)}
                  className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-3 py-2.5 text-sm text-brand-navy dark:text-brand-dark-navy focus:outline-none focus:border-brand-blue font-mono"
                >
                  <option value="">select…</option>
                  {byokProviders.map(id => <option key={id} value={id}>{providerLabel(id)}</option>)}
                </select>
              </div>
              <div>
                <p className="annotation mb-1.5">// model</p>
                <select
                  value={byokModel}
                  onChange={e => setByokModel(e.target.value)}
                  disabled={!byokProvider}
                  className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-3 py-2.5 text-sm text-brand-navy dark:text-brand-dark-navy focus:outline-none focus:border-brand-blue font-mono disabled:opacity-40"
                >
                  {byokModels.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>
            </div>
            <div>
              <p className="annotation mb-1.5">// {providerLabel(byokProvider) || 'provider'} API key</p>
              <div className="relative">
                <input
                  type={showKey ? 'text' : 'password'}
                  value={byokKey}
                  onChange={e => setByokKey(e.target.value)}
                  placeholder="sk-…"
                  className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 pr-16 text-sm font-mono text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue"
                />
                <button
                  onClick={() => setShowKey(v => !v)}
                  className="absolute right-4 top-1/2 -translate-y-1/2 annotation hover:text-brand-navy dark:hover:text-brand-dark-navy"
                >
                  {showKey ? 'hide' : 'show'}
                </button>
              </div>
              <p className="annotation text-brand-muted dark:text-brand-dark-muted mt-1.5">
                // used only for this session — never saved to your account, never logged
              </p>
            </div>
          </div>
        )}
      </div>

      {/* ── Session totals ── */}
      <div className="grid grid-cols-3 gap-3">
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-4">
          <p className="font-mono text-2xl font-medium text-brand-blue tabular-nums">{Math.round(totals.saved).toLocaleString()}</p>
          <p className="annotation mt-1">// provider input tokens avoided</p>
        </div>
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-4">
          <p className="font-mono text-2xl font-medium text-brand-teal tabular-nums">{fmtUsd(totals.cost)}</p>
          <p className="annotation mt-1">// estimated cost delta · ≈ gpt-4o</p>
        </div>
        <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-4">
          <p className="font-mono text-2xl font-medium text-brand-navy dark:text-brand-dark-navy tabular-nums">{totals.hits}<span className="text-base text-brand-muted dark:text-brand-dark-muted"> / {totals.n}</span></p>
          <p className="annotation mt-1">// model calls avoided</p>
        </div>
      </div>

      {/* ── Chat transcript ── */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="annotation">// conversation</p>
          {turns.length > 0 && (
            <button onClick={reset} className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy">clear</button>
          )}
        </div>
        <div
          ref={scrollRef}
          className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 space-y-5 max-h-[520px] overflow-y-auto"
        >
          {turns.length === 0 && !streaming && (
            <div className="text-center py-12 space-y-4">
              <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid">Start the conversation.</p>
              <div className="flex flex-wrap gap-2 justify-center">
                {SUGGESTIONS.map(s => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="font-mono text-xs px-3 py-1.5 rounded-lg border border-brand-border dark:border-brand-dark-border text-brand-navy dark:text-brand-dark-navy hover:border-brand-blue transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t, i) => (
            t.role === 'user' ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[80%] bg-brand-blue text-white rounded-2xl rounded-br-sm px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
                  {t.content}
                </div>
              </div>
            ) : (
              <div key={i} className="space-y-2.5">
                {t.meta && <SavingsStrip meta={t.meta} />}
                <div className="flex justify-start">
                  <div className={`max-w-[85%] rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap border ${
                    t.empty
                      ? 'border-dashed border-brand-border dark:border-brand-dark-border text-brand-muted dark:text-brand-dark-muted'
                      : 'border-brand-border dark:border-brand-dark-border bg-brand-bg dark:bg-brand-dark-bg text-brand-navy dark:text-brand-dark-navy'
                  }`}>
                    {t.content}
                    {t.meta?.model && !t.empty && (
                      <span className="block annotation mt-2">// {providerLabel(t.meta.provider)} / {t.meta.model}</span>
                    )}
                  </div>
                </div>
              </div>
            )
          ))}

          {/* Live in-flight turn */}
          {streaming && (
            <div className="space-y-2.5">
              {pending
                ? <SavingsStrip meta={pending} live />
                : <p className="annotation">// retrieving context…</p>}
              {pending && (
                <div className="flex items-center gap-2 px-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-brand-blue animate-pulse" />
                  <p className="annotation">generating reply…</p>
                </div>
              )}
            </div>
          )}

          {error && <p className="font-mono text-xs text-red-500">✗ {error}</p>}
        </div>

        {/* Composer */}
        <div className="mt-3 flex gap-3">
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (canSend) send() } }}
            placeholder={mode === 'byok' && !byokReady ? 'Add a provider, model, and key above…' : 'Send a message…'}
            className="flex-1 bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
          />
          <button
            onClick={() => send()}
            disabled={!canSend}
            className="bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-6 py-3 text-sm font-medium transition-colors disabled:opacity-40"
          >
            {streaming ? '…' : 'Send'}
          </button>
        </div>
        <p className="annotation mt-2">// tip — repeat the same eligible message to see an exact cache hit reported as one model call avoided</p>
      </div>

      {/* ── Integration guide ── */}
      <div className="space-y-4 pt-2">
        <div className="flex items-center gap-4">
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
          <span className="annotation">// wire it into your own agents</span>
          <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        </div>
        <p className="text-brand-muted dark:text-brand-dark-muted text-sm leading-relaxed max-w-xl mx-auto text-center">
          Call <code className="font-mono text-brand-blue text-xs">/v1/compress</code> before each agent hop —
          replace raw context with the returned <code className="font-mono text-brand-blue text-xs">compressed_messages</code> +{' '}
          <code className="font-mono text-brand-blue text-xs">pruned_context</code>. See the Docs tab for the full SDK.
        </p>
      </div>
    </div>
  )
}
