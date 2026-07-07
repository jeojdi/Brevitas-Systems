import { useState, useRef, useEffect } from 'react'

const STORAGE_KEY = 'bvt_playground'

function loadSaved() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) ?? {} } catch { return {} }
}

function usePersisted(key, defaultValue) {
  const saved = loadSaved()
  const [value, setValue] = useState(key in saved ? saved[key] : defaultValue)
  useEffect(() => {
    const current = loadSaved()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...current, [key]: value }))
  }, [key, value])
  return [value, setValue]
}

const DEMO_MESSAGES = `Agent 1 (Planner): The user wants a rate limiter for their API gateway. I have analyzed the requirements. The rate limiter should use a token bucket algorithm with Redis as the backing store. Each user gets 100 requests per minute. I have analyzed the user request and determined we need a token bucket rate limiter backed by Redis.
Agent 2 (Architect): Based on the planner's analysis, we need a Redis-backed token bucket. The implementation should use Redis to store bucket state per user. Each user gets 100 tokens per minute refill rate. I recommend using GCRA (Generic Cell Rate Algorithm) for atomic operations in Redis. We need Redis for the token bucket state storage.
Agent 3 (Reviewer): The architect recommended Redis with GCRA. I reviewed the approach and approve using Redis with GCRA for atomic token bucket operations. Redis will store the bucket state. 100 tokens per minute per user is correct. One concern: the 120-second TTL may be too aggressive for normal usage patterns.`

const DEMO_CONTEXT = `User is building a Python API gateway service.
User is building a Python API gateway service.
The gateway currently handles 5000 requests per second at peak load.
The gateway currently handles around 5000 req/s at peak.
Rate limiting is needed to prevent abuse from individual clients.
Rate limiting must be enforced to stop client abuse.
Redis 7.2 is already deployed in the infrastructure.
The team uses Redis 7.2 in their existing infrastructure stack.
Previous sprint established that all rate limiting must be atomic to prevent race conditions.
Atomicity is required for the rate limiting implementation to avoid race conditions.`

function Label({ children }) {
  return <p className="annotation mb-1.5">{children}</p>
}

function CodeBlock({ label, code }) {
  const [copied, setCopied] = useState(false)
  const copy = () => { navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 2000) }
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <p className="annotation">{label}</p>
        <button onClick={copy} className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors">
          {copied ? 'copied!' : 'copy'}
        </button>
      </div>
      <pre className="bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl p-5 text-xs font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid overflow-x-auto leading-relaxed whitespace-pre-wrap">
        {code}
      </pre>
    </div>
  )
}

function TokenBar({ baseline, optimized, animate }) {
  const pct = baseline > 0 ? Math.max(4, Math.round((optimized / baseline) * 100)) : 50
  return (
    <div className="space-y-2">
      <div className="flex justify-between">
        <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">{baseline} tokens</span>
        <span className="font-mono text-xs text-brand-blue font-medium">{optimized} tokens</span>
      </div>
      <div className="h-1.5 bg-brand-border dark:bg-brand-dark-border rounded-full overflow-hidden">
        <div
          className="h-full bg-brand-blue rounded-full transition-all duration-700"
          style={{ width: animate ? `${pct}%` : '100%' }}
        />
      </div>
      <div className="flex justify-between annotation">
        <span>before</span><span>after</span>
      </div>
    </div>
  )
}

function StageCard({ children, fade = true }) {
  return (
    <div
      className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 space-y-3"
      style={{ animation: fade ? 'fadeSlideIn 0.3s ease both' : undefined }}
    >
      {children}
    </div>
  )
}

export default function Playground({ apiKey }) {
  const [task, setTask]                    = usePersisted('task', 'Implement a Redis-backed rate limiter for the API gateway')
  const [messages, setMessages]            = usePersisted('messages', DEMO_MESSAGES)
  const [context, setContext]              = usePersisted('context', DEMO_CONTEXT)
  const [complexity, setComplexity]        = usePersisted('complexity', 0.5)
  const [compressionLevel, setCompression] = usePersisted('compressionLevel', 2)
  const [pruneBudget, setPruneBudget]      = usePersisted('pruneBudget', 5)
  const [stages, setStages]               = usePersisted('stages', [])
  const [streaming, setStreaming]          = useState(false)
  const [error, setError]                  = useState('')
  const abortRef                           = useRef(null)

  const addStage = (event) => setStages(prev => [...prev, event])

  const run = async () => {
    if (abortRef.current) abortRef.current.abort()
    const controller = new AbortController()
    abortRef.current = controller

    setStreaming(true)
    setError('')
    setStages([])

    try {
      const res = await fetch('/v1/compress/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({
          task,
          messages:          messages.split('\n').map(s => s.trim()).filter(Boolean),
          prior_context:     context.split('\n').map(s => s.trim()).filter(Boolean),
          complexity,
          compression_level: compressionLevel,
          prune_budget:      pruneBudget,
        }),
        signal: controller.signal,
      })

      if (!res.ok) throw new Error(await res.text())

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop()
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.stage === 'error') { setError(event.message); break }
            addStage(event)
          } catch { /* malformed chunk, skip */ }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') setError(e.message)
    } finally {
      setStreaming(false)
    }
  }

  const routed    = stages.find(s => s.stage === 'routed')
  const compressed = stages.find(s => s.stage === 'compressed')
  const modelEvt  = stages.find(s => s.stage === 'model_response')
  const done      = stages.find(s => s.stage === 'done')
  const result    = done?.result

  const pythonSnippet =
`import requests

def compress(messages, prior_context, task=""):
    r = requests.post(
        "http://localhost:8000/v1/compress",
        headers={"X-API-Key": "YOUR_API_KEY"},
        json={
            "messages": messages,
            "prior_context": prior_context,
            "task": task,
            "compression_level": ${compressionLevel},
            "prune_budget": ${pruneBudget},
        },
    )
    r.raise_for_status()
    d = r.json()
    # pass d["compressed_messages"] + d["pruned_context"]
    # to your next agent — not the raw context
    return d`

  const curlSnippet =
`curl -X POST http://localhost:8000/v1/compress \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "task": "your task here",
    "messages": ["agent output 1", "agent output 2"],
    "prior_context": ["context chunk 1"],
    "compression_level": ${compressionLevel},
    "prune_budget": ${pruneBudget}
  }'`

  return (
    <div className="space-y-14">
      <style>{`
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(8px); }
          to   { opacity: 1; transform: translateY(0); }
        }
      `}</style>

      {/* ── Hero text ── */}
      <div>
        <p className="annotation tracking-widest uppercase mb-4">Playground</p>
        <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight">
          Pick a task. Feed it to Brevitas.<br />
          <em className="italic text-brand-teal">Watch the tokens drop.</em>
        </h2>
        <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-4 max-w-lg leading-relaxed">
          Paste your agent messages and prior context below. The pipeline compresses, prunes, and
          references — your next agent gets only what it needs.
        </p>
      </div>

      {/* ── Input / Output grid ── */}
      <div className="grid lg:grid-cols-2 gap-8">
        {/* Input */}
        <div className="space-y-5">
          <div>
            <Label>// current task</Label>
            <input
              value={task}
              onChange={e => setTask(e.target.value)}
              className="w-full bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue transition-colors"
              placeholder="Describe the current task…"
            />
          </div>

          <div>
            <Label>// agent messages — one per line</Label>
            <textarea
              value={messages}
              onChange={e => setMessages(e.target.value)}
              rows={7}
              className="w-full bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue font-mono resize-y transition-colors leading-relaxed"
            />
          </div>

          <div>
            <Label>// prior context — one per line</Label>
            <textarea
              value={context}
              onChange={e => setContext(e.target.value)}
              rows={5}
              className="w-full bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3 text-sm text-brand-navy dark:text-brand-dark-navy placeholder-brand-muted dark:placeholder-brand-dark-muted focus:outline-none focus:border-brand-blue font-mono resize-y transition-colors leading-relaxed"
            />
          </div>

          {/* Settings */}
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-5 space-y-4">
            <p className="annotation">// settings</p>
            <div>
              <div className="flex justify-between annotation mb-2">
                <span>task complexity</span>
                <span className="text-brand-navy dark:text-brand-dark-navy">{complexity.toFixed(1)}</span>
              </div>
              <input
                type="range" min="0" max="1" step="0.1" value={complexity}
                onChange={e => setComplexity(parseFloat(e.target.value))}
                className="w-full accent-brand-blue"
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>// compression level</Label>
                <select
                  value={compressionLevel}
                  onChange={e => setCompression(Number(e.target.value))}
                  className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-3 py-2.5 text-sm text-brand-navy dark:text-brand-dark-navy focus:outline-none focus:border-brand-blue font-mono"
                >
                  <option value={1}>1 — light</option>
                  <option value={2}>2 — medium</option>
                  <option value={3}>3 — aggressive</option>
                </select>
              </div>
              <div>
                <Label>// prune budget</Label>
                <select
                  value={pruneBudget}
                  onChange={e => setPruneBudget(Number(e.target.value))}
                  className="w-full bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-3 py-2.5 text-sm text-brand-navy dark:text-brand-dark-navy focus:outline-none focus:border-brand-blue font-mono"
                >
                  <option value={3}>3 chunks</option>
                  <option value={5}>5 chunks</option>
                  <option value={8}>8 chunks</option>
                </select>
              </div>
            </div>
          </div>

          <button
            onClick={run}
            disabled={streaming}
            className="w-full bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-4 py-3.5 text-sm font-medium transition-colors disabled:opacity-50"
          >
            {streaming ? 'Running pipeline…' : 'Compress →'}
          </button>
        </div>

        {/* Output — real-time stages */}
        <div className="space-y-4">
          {/* Empty state */}
          {stages.length === 0 && !streaming && !error && (
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-20 text-center h-full flex flex-col items-center justify-center">
              <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-2">Results appear here.</p>
              <p className="annotation">// hit compress to run the pipeline</p>
            </div>
          )}

          {/* Streaming spinner (shown until first stage arrives) */}
          {streaming && stages.length === 0 && (
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-10 text-center flex flex-col items-center justify-center gap-2">
              <p className="annotation">// routing…</p>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="bg-white dark:bg-brand-dark-surface border border-red-200 dark:border-red-900/40 rounded-2xl p-5">
              <p className="font-mono text-xs text-red-500">{error}</p>
            </div>
          )}

          {/* Stage 1: Routing */}
          {routed && (
            <StageCard>
              <div className="flex items-center justify-between">
                <p className="annotation">// routing</p>
                <span className="w-2 h-2 rounded-full bg-brand-teal shrink-0" />
              </div>
              <p className="font-mono text-xs text-brand-navy dark:text-brand-dark-navy">
                model → <span className="text-brand-blue">{routed.model}</span>
              </p>
              <div className="h-1 bg-brand-border dark:bg-brand-dark-border rounded-full overflow-hidden">
                <div className="h-full bg-brand-teal rounded-full" style={{ width: `${Math.round(routed.route_fit * 100)}%` }} />
              </div>
              <p className="annotation">route fit {(routed.route_fit * 100).toFixed(0)}%</p>
            </StageCard>
          )}

          {/* Stage 2: Compression + Pruning */}
          {compressed && (
            <StageCard>
              <div className="flex items-center justify-between">
                <p className="annotation">// compression + pruning</p>
                {streaming && !result && <span className="annotation">running model…</span>}
              </div>

              {/* Live token bar */}
              <TokenBar
                baseline={compressed.baseline_tokens}
                optimized={compressed.optimized_tokens}
                animate
              />

              {/* Savings numbers */}
              <div className="grid grid-cols-2 gap-4 pt-1">
                <div>
                  <p className="font-mono text-3xl font-medium text-brand-blue tabular-nums">
                    {compressed.savings_pct.toFixed(1)}%
                  </p>
                  <p className="annotation mt-0.5">// tokens saved</p>
                </div>
                <div>
                  <p className="font-mono text-3xl font-medium text-brand-teal tabular-nums">
                    {compressed.quality_proxy != null
                      ? `${(compressed.quality_proxy * 100).toFixed(1)}%`
                      : 'lossless'}
                  </p>
                  <p className="annotation mt-0.5">// context retained</p>
                </div>
              </div>

              {/* Compressed messages */}
              {compressed.compressed_messages?.length > 0 && (
                <div className="pt-1 space-y-1.5">
                  <p className="annotation">// compressed messages ({compressed.compressed_messages.length})</p>
                  {compressed.compressed_messages.map((m, i) => (
                    <p key={i} className="text-xs font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid bg-brand-bg dark:bg-brand-dark-bg rounded-xl p-3 leading-relaxed">
                      {m}
                    </p>
                  ))}
                </div>
              )}

              {/* Retained context */}
              {compressed.pruned_context?.length > 0 && (
                <div className="pt-1 space-y-1.5">
                  <p className="annotation">// retained context ({compressed.pruned_context.length})</p>
                  {compressed.pruned_context.map((c, i) => (
                    <p key={i} className="text-xs font-mono text-brand-teal bg-brand-teal-dim dark:bg-brand-dark-teal-dim rounded-xl p-3 leading-relaxed">
                      {c}
                    </p>
                  ))}
                </div>
              )}
            </StageCard>
          )}

          {/* Stage 3: Model response (if model is configured) */}
          {modelEvt?.text && (
            <StageCard>
              <p className="annotation">// model response</p>
              <p className="text-xs font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid bg-brand-bg dark:bg-brand-dark-bg rounded-xl p-3 leading-relaxed whitespace-pre-wrap">
                {modelEvt.text}
              </p>
            </StageCard>
          )}

          {/* Stage 4: Final summary pill */}
          {result && (
            <StageCard fade>
              <div className="flex items-center justify-between">
                <p className="annotation">// done</p>
                <span className="font-mono text-xs text-brand-teal">✓ pipeline complete</span>
              </div>
              <p className="annotation">
                routed → <span className="text-brand-blue">{result.routed_model_hint}</span>
                {result.state_id && (
                  <> · state <span className="text-brand-muted dark:text-brand-dark-muted">{result.state_id.slice(0, 14)}…</span></>
                )}
              </p>
            </StageCard>
          )}

          {/* Model still running indicator */}
          {streaming && compressed && !result && (
            <div className="flex items-center gap-2 px-1">
              <span className="w-1.5 h-1.5 rounded-full bg-brand-blue animate-pulse" />
              <p className="annotation">calling model…</p>
            </div>
          )}
        </div>
      </div>

      {/* ── Divider ── */}
      <div className="flex items-center gap-4">
        <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
        <span className="annotation">// and no one optimized what flows between them</span>
        <div className="flex-1 h-px bg-brand-border dark:bg-brand-dark-border" />
      </div>

      {/* ── Integration guide ── */}
      <div className="space-y-8">
        <div>
          <p className="annotation tracking-widest uppercase mb-2">Integration Guide</p>
          <p className="font-serif text-3xl text-brand-navy dark:text-brand-dark-navy">
            Drop it in front of any agent hop.
          </p>
          <p className="text-brand-muted dark:text-brand-dark-muted mt-3 text-sm leading-relaxed max-w-xl">
            Call <code className="font-mono text-brand-blue text-xs">/v1/compress</code> before
            passing messages between agents. Replace raw context with the returned{' '}
            <code className="font-mono text-brand-blue text-xs">compressed_messages</code> +{' '}
            <code className="font-mono text-brand-blue text-xs">pruned_context</code>.
            No changes to your agents, prompts, or provider.
          </p>
        </div>
        <CodeBlock label="// python" code={pythonSnippet} />
        <CodeBlock label="// curl"   code={curlSnippet}   />
      </div>
    </div>
  )
}
