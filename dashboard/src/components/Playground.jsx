import { useState } from 'react'

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

function TokenBar({ baseline, optimized }) {
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
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between annotation">
        <span>before</span><span>after</span>
      </div>
    </div>
  )
}

export default function Playground({ apiKey }) {
  const [task, setTask]                    = useState('Write a Python sort utility function')
  const [messages, setMessages]            = useState(DEMO_MESSAGES)
  const [context, setContext]              = useState(DEMO_CONTEXT)
  const [complexity, setComplexity]        = useState(0.5)
  const [compressionLevel, setCompression] = useState(2)
  const [pruneBudget, setPruneBudget]      = useState(5)
  const [loading, setLoading]              = useState(false)
  const [result, setResult]                = useState(null)
  const [error, setError]                  = useState('')

  const run = async () => {
    setLoading(true); setError(''); setResult(null)
    try {
      const res = await fetch('/v1/compress', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
        body: JSON.stringify({
          task,
          messages:      messages.split('\n').map(s => s.trim()).filter(Boolean),
          prior_context: context.split('\n').map(s => s.trim()).filter(Boolean),
          complexity,
          compression_level: compressionLevel,
          prune_budget: pruneBudget,
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      setResult(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const pythonSnippet =
`import requests

def compress(messages, prior_context, task=""):
    r = requests.post(
        "http://localhost:8000/v1/compress",
        headers={"X-API-Key": "${apiKey}"},
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
  -H "X-API-Key: ${apiKey}" \\
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
            disabled={loading}
            className="w-full bg-brand-blue hover:bg-brand-navy text-white rounded-xl px-4 py-3.5 text-sm font-medium transition-colors disabled:opacity-50"
          >
            {loading ? 'Running pipeline…' : 'Compress →'}
          </button>
        </div>

        {/* Output */}
        <div className="space-y-5">
          {!result && !error && !loading && (
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-20 text-center h-full flex flex-col items-center justify-center">
              <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-2">Results appear here.</p>
              <p className="annotation">// hit compress to run the pipeline</p>
            </div>
          )}

          {loading && (
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-20 text-center h-full flex flex-col items-center justify-center">
              <p className="annotation">// running pipeline…</p>
            </div>
          )}

          {error && (
            <div className="bg-white dark:bg-brand-dark-surface border border-red-200 dark:border-red-900/40 rounded-2xl p-5">
              <p className="font-mono text-xs text-red-500">{error}</p>
            </div>
          )}

          {result && (
            <div className="space-y-4">
              {/* Big savings numbers */}
              <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-7">
                <div className="grid grid-cols-2 gap-6 mb-7">
                  <div>
                    <p className="font-mono text-5xl font-medium text-brand-blue tabular-nums">
                      {result.savings_pct.toFixed(1)}%
                    </p>
                    <p className="annotation mt-1">// tokens saved</p>
                  </div>
                  <div>
                    <p className="font-mono text-5xl font-medium text-brand-teal tabular-nums">
                      {(result.quality_proxy * 100).toFixed(1)}%
                    </p>
                    <p className="annotation mt-1">// context retained</p>
                  </div>
                </div>
                <TokenBar baseline={result.baseline_tokens} optimized={result.optimized_tokens} />
              </div>

              {/* Compressed messages */}
              {result.compressed_messages?.length > 0 && (
                <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5">
                  <p className="annotation mb-3">
                    // compressed messages ({result.compressed_messages.length})
                  </p>
                  <div className="space-y-2">
                    {result.compressed_messages.map((m, i) => (
                      <p key={i} className="text-xs font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid bg-brand-bg dark:bg-brand-dark-bg rounded-xl p-3 leading-relaxed">
                        {m}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Retained context */}
              {result.pruned_context?.length > 0 && (
                <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5">
                  <p className="annotation mb-3">
                    // retained context ({result.pruned_context.length})
                  </p>
                  <div className="space-y-2">
                    {result.pruned_context.map((c, i) => (
                      <p key={i} className="text-xs font-mono text-brand-teal bg-brand-teal-dim dark:bg-brand-dark-teal-dim rounded-xl p-3 leading-relaxed">
                        {c}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Routing info */}
              <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl px-5 py-3">
                <p className="annotation">
                  routed → <span className="text-brand-blue">{result.routed_model_hint}</span>
                  {result.state_id && (
                    <> · state <span className="text-brand-muted dark:text-brand-dark-muted">{result.state_id.slice(0, 14)}…</span></>
                  )}
                </p>
              </div>
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
