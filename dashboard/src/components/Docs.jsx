import { useState } from 'react'

function Section({ id, title, children }) {
  return (
    <section id={id} className="space-y-5 scroll-mt-24">
      <h3 className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy border-b border-brand-border dark:border-brand-dark-border pb-3">
        {title}
      </h3>
      {children}
    </section>
  )
}

function Endpoint({ method, path, description }) {
  const colors = {
    POST: 'bg-brand-blue text-white',
    GET:  'bg-brand-teal dark:bg-brand-dark-teal text-white',
    PUT:  'bg-amber-500 text-white',
  }
  return (
    <div className="flex items-center gap-3 font-mono text-sm">
      <span className={`px-2 py-0.5 rounded text-xs font-bold ${colors[method]}`}>{method}</span>
      <span className="text-brand-navy dark:text-brand-dark-navy">{path}</span>
      {description && <span className="text-brand-muted dark:text-brand-dark-muted text-xs">— {description}</span>}
    </div>
  )
}

function Field({ name, type, required, children }) {
  return (
    <tr className="border-t border-brand-border dark:border-brand-dark-border">
      <td className="py-2.5 pr-4 font-mono text-xs text-brand-blue whitespace-nowrap">{name}</td>
      <td className="py-2.5 pr-4 font-mono text-xs text-brand-muted dark:text-brand-dark-muted whitespace-nowrap">{type}</td>
      <td className="py-2.5 pr-4 text-xs whitespace-nowrap">
        {required
          ? <span className="text-brand-blue font-medium">required</span>
          : <span className="text-brand-muted dark:text-brand-dark-muted">optional</span>}
      </td>
      <td className="py-2.5 text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{children}</td>
    </tr>
  )
}

function CodeBlock({ lang, code }) {
  const [copied, setCopied] = useState(false)
  const copy = () => { navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 2000) }
  return (
    <div className="relative rounded-xl overflow-hidden">
      <div className="flex items-center justify-between bg-[#0c0c0c] dark:bg-[#080808] px-4 py-2 border-b border-[#222]">
        <span className="font-mono text-[10px] text-[#555] tracking-widest uppercase">{lang}</span>
        <button onClick={copy} className="font-mono text-[10px] text-[#555] hover:text-[#aaa] transition-colors">
          {copied ? 'copied!' : 'copy'}
        </button>
      </div>
      <pre className="bg-[#0c0c0c] dark:bg-[#080808] p-5 text-xs font-mono text-[#ccc] overflow-x-auto leading-relaxed whitespace-pre">
        {code}
      </pre>
    </div>
  )
}

const NAV = [
  { id: 'overview',    label: 'Overview' },
  { id: 'auth',        label: 'Authentication' },
  { id: 'compress',    label: 'POST /v1/compress' },
  { id: 'stats',       label: 'GET /v1/stats' },
  { id: 'provider',    label: 'Provider config' },
  { id: 'keys',        label: 'Key management' },
  { id: 'examples',    label: 'Code examples' },
  { id: 'deployment',  label: 'Deployment' },
]

export default function Docs({ apiKey }) {
  const BASE = 'http://localhost:8000'

  return (
    <div className="flex gap-12">
      {/* Sticky sidebar */}
      <aside className="hidden lg:block w-44 shrink-0">
        <div className="sticky top-24 space-y-1">
          <p className="annotation tracking-widest uppercase mb-3">On this page</p>
          {NAV.map(n => (
            <a
              key={n.id}
              href={`#${n.id}`}
              className="block text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors py-0.5"
            >
              {n.label}
            </a>
          ))}
        </div>
      </aside>

      {/* Content */}
      <div className="flex-1 space-y-16 min-w-0">

        <div>
          <p className="annotation tracking-widest uppercase mb-4">API Reference</p>
          <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight mb-4">
            Brevitas API
          </h2>
          <p className="text-brand-muted dark:text-brand-dark-muted text-base leading-relaxed max-w-xl">
            Compress, prune, and reference agent context before passing it between models.
            One call between agent hops — no changes to your agents or provider.
          </p>
          <p className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted mt-4">
            Base URL: <span className="text-brand-navy dark:text-brand-dark-navy">{BASE}</span>
          </p>
        </div>

        {/* Overview */}
        <Section id="overview" title="Overview">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Brevitas sits between agent hops. Instead of forwarding raw accumulated context to the next model,
            POST it to <code className="font-mono text-brand-blue text-xs">/v1/compress</code>.
            The pipeline compresses messages, prunes low-relevance context, and returns only what the
            next agent needs — with exact token counts before and after.
          </p>
          <div className="bg-brand-bg dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-5">
            <p className="annotation mb-3">// typical flow</p>
            <pre className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid leading-relaxed whitespace-pre">{`agent_1_output  ─┐
prior_context   ─┤─▶  POST /v1/compress  ─▶  compressed_messages
new_task        ─┘                            pruned_context
                                              savings_pct
                                                   │
                                                   ▼
                                             agent_2 (receives less)`}</pre>
          </div>
          <div className="grid sm:grid-cols-3 gap-4">
            {[
              ['Compression', 'Rewrites verbose agent messages to be semantically dense.'],
              ['Context pruning', 'Scores and keeps only the most task-relevant prior context chunks.'],
              ['Token accounting', 'Returns exact before/after token counts on every call.'],
            ].map(([title, desc]) => (
              <div key={title} className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-4">
                <p className="text-sm font-medium text-brand-navy dark:text-brand-dark-navy mb-1">{title}</p>
                <p className="text-xs text-brand-muted dark:text-brand-dark-muted leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>
        </Section>

        {/* Auth */}
        <Section id="auth" title="Authentication">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Every request except <code className="font-mono text-brand-blue text-xs">POST /v1/keys</code> and{' '}
            <code className="font-mono text-brand-blue text-xs">GET /v1/health</code> requires a Brevitas API key
            in the <code className="font-mono text-brand-blue text-xs">X-API-Key</code> header.
            Keys are hashed with SHA-256 before storage — the raw key is returned once at creation time only.
          </p>
          <CodeBlock lang="http" code={`X-API-Key: bvt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`} />
          {apiKey && (
            <p className="text-xs text-brand-muted dark:text-brand-dark-muted">
              Your current key: <span className="font-mono text-brand-blue">{apiKey}</span>
            </p>
          )}
        </Section>

        {/* Compress */}
        <Section id="compress" title="Compress context">
          <Endpoint method="POST" path="/v1/compress" description="main endpoint — compress + prune agent context" />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Runs the full compression pipeline and returns optimised text plus real token savings.
            The configured model backend receives the compressed prompt; its response is included in the reply.
            Rate limited to <strong className="text-brand-navy dark:text-brand-dark-navy">60 requests/minute</strong> per key.
          </p>

          <p className="annotation mt-4 mb-2">// request body</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <tbody>
                <Field name="messages"          type="string[]" required>Agent outputs to compress. Max 100 elements, 50 k chars each.</Field>
                <Field name="prior_context"     type="string[]" required={false}>Context chunks to prune. Max 200 elements.</Field>
                <Field name="task"              type="string"   required={false}>Current task description — used to score context relevance. Max 2,000 chars.</Field>
                <Field name="complexity"        type="float"    required={false}>Task complexity 0–1. Higher = more context kept. Default 0.5.</Field>
                <Field name="urgency"           type="float"    required={false}>Urgency 0–1. Higher = favour recency in pruning. Default 0.5.</Field>
                <Field name="compression_level" type="int"      required={false}>1 light · 2 medium · 3 aggressive. Default 2.</Field>
                <Field name="prune_budget"      type="int"      required={false}>Max context chunks to keep (1–50). Default 5.</Field>
                <Field name="delta_mode"        type="string"   required={false}>"off" | "on". Sends delta patches after turn 1 to further reduce payload size.</Field>
                <Field name="wire_mode"         type="string"   required={false}>"json" | "msgpack". Internal protocol wire format.</Field>
              </tbody>
            </table>
          </div>

          <p className="annotation mt-6 mb-2">// response</p>
          <CodeBlock lang="json" code={`{
  "compressed_messages": ["…"],   // rewritten, shorter versions of your messages
  "pruned_context":      ["…"],   // context chunks that survived pruning
  "baseline_tokens":     312,     // token count BEFORE compression
  "optimized_tokens":    118,     // token count AFTER compression
  "savings_pct":         62.18,   // (baseline - optimized) / baseline × 100
  "quality_proxy":       0.9731,  // context-retention score 0–1
  "routed_model_hint":   "llama-large",  // model tier selected by router
  "model_response":      "…",     // response from your configured model backend
  "state_id":            "abc123…"       // snapshot id (used by delta mode)
}`} />
        </Section>

        {/* Stats */}
        <Section id="stats" title="Usage stats">
          <Endpoint method="GET" path="/v1/stats" description="aggregated token savings for your key" />
          <CodeBlock lang="json" code={`{
  "total_calls":           42,
  "total_tokens_saved":    18420,
  "avg_savings_pct":       61.4,
  "avg_quality_proxy":     0.971,
  "total_baseline_tokens": 30000,
  "total_optimized_tokens": 11580,
  "history": [
    {
      "timestamp":        "2026-06-11T14:22:01+00:00",
      "baseline_tokens":  312,
      "optimized_tokens": 118,
      "savings_pct":      62.18,
      "quality_proxy":    0.9731
    }
  ]
}`} />
        </Section>

        {/* Provider */}
        <Section id="provider" title="Provider config">
          <div className="space-y-6">
            <div className="space-y-2">
              <Endpoint method="GET" path="/v1/provider" description="get current model backend" />
              <CodeBlock lang="json" code={`{
  "provider":    "anthropic",
  "model":       "claude-sonnet-4-6",
  "has_api_key": true,
  "masked_key":  "********3x9f"
}`} />
            </div>

            <div className="space-y-2">
              <Endpoint method="PUT" path="/v1/provider" description="set model backend" />
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <tbody>
                    <Field name="provider"         type="string" required>ollama · anthropic · openai · grok · deepseek</Field>
                    <Field name="model"            type="string" required>Model name for the chosen provider.</Field>
                    <Field name="provider_api_key" type="string" required={false}>Provider API key. Required for all except ollama. Stored encrypted at rest.</Field>
                  </tbody>
                </table>
              </div>
            </div>

            <div className="space-y-2">
              <Endpoint method="GET" path="/v1/providers" description="list all supported providers and models" />
            </div>
          </div>
        </Section>

        {/* Keys */}
        <Section id="keys" title="Key management">
          <div className="space-y-2">
            <Endpoint method="POST" path="/v1/keys" description="create a Brevitas API key — no auth required · 10/min" />
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <tbody>
                  <Field name="name" type="string" required={false}>Human-readable label. Max 100 chars. Default "default".</Field>
                </tbody>
              </table>
            </div>
            <CodeBlock lang="json" code={`// response — the api_key is shown once, store it immediately
{
  "api_key": "bvt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "name":    "my-agent-project"
}`} />
          </div>
          <div className="mt-4">
            <Endpoint method="GET" path="/v1/keys" description="list keys (names + dates, no raw values)" />
          </div>
        </Section>

        {/* Examples */}
        <Section id="examples" title="Code examples">
          <CodeBlock lang="python" code={`import requests

BREVITAS_KEY = "bvt_your_key_here"
BASE         = "http://localhost:8000"
HEADERS      = {"X-API-Key": BREVITAS_KEY, "Content-Type": "application/json"}

def compress(messages: list[str], prior_context: list[str], task: str = "") -> dict:
    r = requests.post(
        f"{BASE}/v1/compress",
        headers=HEADERS,
        json={
            "messages":          messages,
            "prior_context":     prior_context,
            "task":              task,
            "compression_level": 2,
            "prune_budget":      5,
        },
    )
    r.raise_for_status()
    return r.json()

# In your agent pipeline:
result = compress(
    messages=["Agent 1 produced a long analysis of the user's data pipeline…"],
    prior_context=["User is on Python 3.11.", "Project uses pandas.", "Tests use pytest."],
    task="Write a data validation function",
)

# Pass these to agent 2 — NOT the raw context
agent2_input = result["compressed_messages"] + result["pruned_context"]
print(f"Saved {result['savings_pct']}%  |  retained {result['quality_proxy']*100:.1f}% context")`} />

          <CodeBlock lang="javascript" code={`const BREVITAS_KEY = "bvt_your_key_here";
const BASE = "http://localhost:8000";

async function compress(messages, priorContext, task = "") {
  const res = await fetch(\`\${BASE}/v1/compress\`, {
    method: "POST",
    headers: {
      "X-API-Key": BREVITAS_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messages,
      prior_context: priorContext,
      task,
      compression_level: 2,
      prune_budget: 5,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

const result = await compress(
  ["Agent 1 output…"],
  ["Context chunk 1", "Context chunk 2"],
  "Generate a report"
);

// Feed these to your next agent
const agent2Input = [...result.compressed_messages, ...result.pruned_context];
console.log(\`Saved \${result.savings_pct}% · \${result.optimized_tokens} tokens out\`);`} />

          <CodeBlock lang="bash" code={`# Create an API key
curl -X POST http://localhost:8000/v1/keys \\
  -H "Content-Type: application/json" \\
  -d '{"name": "my-project"}'

# Compress context
curl -X POST http://localhost:8000/v1/compress \\
  -H "X-API-Key: bvt_your_key_here" \\
  -H "Content-Type: application/json" \\
  -d '{
    "task": "Write a data validation function",
    "messages": ["Agent 1 produced a long analysis..."],
    "prior_context": ["User is on Python 3.11.", "Project uses pandas."],
    "compression_level": 2,
    "prune_budget": 5
  }'

# Check token savings
curl http://localhost:8000/v1/stats \\
  -H "X-API-Key: bvt_your_key_here"`} />
        </Section>

        {/* Deployment */}
        <Section id="deployment" title="Deployment">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Brevitas is designed to run as a self-hosted sidecar alongside your agent pipeline.
            Configure it with environment variables and put it behind a TLS-terminating proxy for external access.
          </p>

          <div className="space-y-3">
            <p className="annotation">// environment variables</p>
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <tbody>
                  <Field name="BREVITAS_SECRET_KEY" type="string" required={false}>Fernet key for encrypting provider API keys at rest. Auto-generated and saved to .secret_key if not set.</Field>
                  <Field name="ALLOWED_ORIGINS"     type="string" required={false}>Comma-separated CORS origins. Default "*". Set to your frontend URL in production.</Field>
                  <Field name="OLLAMA_HOST"         type="string" required={false}>Ollama base URL. Default http://localhost:11434.</Field>
                </tbody>
              </table>
            </div>
          </div>

          <div className="space-y-3 mt-2">
            <p className="annotation">// start with multiple workers</p>
            <CodeBlock lang="bash" code={`# Single worker (default)
uvicorn api.server:app --host 0.0.0.0 --port 8000

# Multiple workers for concurrent load
uvicorn api.server:app --host 0.0.0.0 --port 8000 --workers 4

# With a custom secret key (recommended for production)
BREVITAS_SECRET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \\
ALLOWED_ORIGINS="https://your-dashboard.example.com" \\
uvicorn api.server:app --host 0.0.0.0 --port 8000 --workers 4`} />
          </div>

          <div className="space-y-3 mt-2">
            <p className="annotation">// TLS via Caddy (recommended)</p>
            <CodeBlock lang="caddy" code={`# Caddyfile
brevitas.yourdomain.com {
    reverse_proxy localhost:8000
}`} />
          </div>

          <div className="space-y-3 mt-2">
            <p className="annotation">// or nginx</p>
            <CodeBlock lang="nginx" code={`server {
    listen 443 ssl;
    server_name brevitas.yourdomain.com;
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}`} />
          </div>
        </Section>

      </div>
    </div>
  )
}
