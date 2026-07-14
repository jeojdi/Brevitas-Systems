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

function Field({ name, type, defaultVal, children }) {
  return (
    <tr className="border-t border-brand-border dark:border-brand-dark-border">
      <td className="py-2.5 pr-4 font-mono text-xs text-brand-blue whitespace-nowrap">{name}</td>
      <td className="py-2.5 pr-4 font-mono text-xs text-brand-muted dark:text-brand-dark-muted whitespace-nowrap">{type}</td>
      <td className="py-2.5 pr-4 font-mono text-xs text-brand-muted dark:text-brand-dark-muted whitespace-nowrap">{defaultVal ?? '—'}</td>
      <td className="py-2.5 text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{children}</td>
    </tr>
  )
}

function FieldHead() {
  return (
    <thead>
      <tr className="text-left">
        <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Parameter</th>
        <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Type</th>
        <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Default</th>
        <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal">Description</th>
      </tr>
    </thead>
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

const SDK_NAV = [
  { id: 'sdk-install',     label: 'Install' },
  { id: 'sdk-auth',        label: 'Authentication' },
  { id: 'sdk-basic',       label: 'Wrap a client' },
  { id: 'sdk-proxy',       label: 'Zero-code proxy' },
  { id: 'sdk-configure',   label: 'configure() params' },
  { id: 'sdk-wrap',        label: 'wrap() params' },
  { id: 'sdk-cli',         label: 'CLI' },
  { id: 'sdk-multiturn',   label: 'Multi-turn sessions' },
  { id: 'sdk-frameworks',  label: 'Frameworks' },
]

const API_NAV = [
  { id: 'api-overview',   label: 'Overview' },
  { id: 'api-auth',       label: 'Authentication' },
  { id: 'api-compress',   label: 'POST /v1/compress' },
  { id: 'api-stats',      label: 'GET /v1/stats' },
  { id: 'api-provider',   label: 'Provider config' },
  { id: 'api-health',     label: 'Health & errors' },
  { id: 'api-example',    label: 'End-to-end example' },
]

function PythonSDKDocs() {
  return (
    <div className="flex gap-12">
      <aside className="hidden lg:block w-44 shrink-0">
        <div className="sticky top-24 space-y-1">
          <p className="annotation tracking-widest uppercase mb-3">On this page</p>
          {SDK_NAV.map(n => (
            <a key={n.id} href={`#${n.id}`}
              className="block text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors py-0.5">
              {n.label}
            </a>
          ))}
        </div>
      </aside>

      <div className="flex-1 space-y-16 min-w-0">
        <div>
          <p className="annotation tracking-widest uppercase mb-4">Python SDK</p>
          <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight mb-4">
            brevitas-systems
          </h2>
          <p className="text-brand-muted dark:text-brand-dark-muted text-base leading-relaxed max-w-xl">
            Cut token cost on every model call — without changing your prompts. Wrap your existing
            OpenAI or Anthropic client with <code className="font-mono text-brand-blue text-sm">brevitas.wrap()</code>,
            or route through the zero-code proxy and change nothing at all.
          </p>
        </div>

        <Section id="sdk-install" title="Install">
          <CodeBlock lang="bash" code={`pip install brevitas-systems==0.9.10`} />
        </Section>

        <Section id="sdk-auth" title="Authentication">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Set your API key as an environment variable (recommended), or pass it directly in code.
            Create and copy a key from the <span className="text-brand-blue">API Keys</span> tab.
          </p>
          <CodeBlock lang="bash" code={`export BREVITAS_API_KEY=bvt_your_key_here
export BREVITAS_BASE_URL=https://brevitassystems.com
export BREVITAS_PROJECT=my-app
export BREVITAS_ENVIRONMENT=production
export BREVITAS_SOURCE=backend`} />
          <CodeBlock lang="python" code={`# or configure in code
from brevitas import configure
configure(api_key="bvt_your_key_here")`} />
        </Section>

        <Section id="sdk-basic" title="Wrap a client">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Pass your existing <code className="font-mono text-brand-blue text-xs">openai.OpenAI</code> or{' '}
            <code className="font-mono text-brand-blue text-xs">anthropic.Anthropic</code> client to{' '}
            <code className="font-mono text-brand-blue text-xs">brevitas.wrap()</code>. You get a drop-in
            replacement that compresses + tracks every call. Your prompts and provider are unchanged.
          </p>
          <CodeBlock lang="python" code={`import openai, brevitas

brevitas.configure(api_key="bvt_your_key_here")
client = brevitas.wrap(openai.OpenAI())   # reads OPENAI_API_KEY from env

resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Summarize the Q3 report"}],
)
print(resp.choices[0].message.content)`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Anthropic works the same way:
          </p>
          <CodeBlock lang="python" code={`import anthropic, brevitas

brevitas.configure(api_key="bvt_your_key_here")
client = brevitas.wrap(anthropic.Anthropic())

resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Summarize the Q3 report"}],
)
print(resp.content[0].text)`} />
        </Section>

        <Section id="sdk-proxy" title="Zero-code proxy">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Don't want to touch code at all? Start the local proxy and point your provider's base URL at it.
            Your existing code runs unchanged — every call is routed through Brevitas.
          </p>
          <CodeBlock lang="bash" code={`brevitas start --api-key bvt_your_key_here --port 4242

export OPENAI_BASE_URL=http://localhost:4242/openai
export ANTHROPIC_BASE_URL=http://localhost:4242
# now run your app as usual`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Or let <code className="font-mono text-brand-blue text-xs">brevitas init</code> find your call
            sites and wire either path in for you.
          </p>
        </Section>

        <Section id="sdk-configure" title="configure() parameters">
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <FieldHead />
              <tbody>
                <Field name="api_key"  type="str"  defaultVal="env var">Your <code className="font-mono text-brand-blue text-xs">bvt_</code> prefixed Brevitas key. Falls back to <code className="font-mono text-xs">BREVITAS_API_KEY</code>.</Field>
                <Field name="base_url" type="str"  defaultVal='"brevitassystems.com"'>Compression engine URL. Point at a local engine for self-hosting.</Field>
                <Field name="enabled"  type="bool" defaultVal="True">Toggle compression off without removing <code className="font-mono text-xs">wrap()</code> from your code.</Field>
                <Field name="timeout"  type="int"  defaultVal="30">Per-request timeout in seconds.</Field>
              </tbody>
            </table>
          </div>
        </Section>

        <Section id="sdk-wrap" title="wrap() parameters">
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <FieldHead />
              <tbody>
                <Field name="client"  type="OpenAI | Anthropic" defaultVal="required">An <code className="font-mono text-xs">openai.OpenAI</code> or <code className="font-mono text-xs">anthropic.Anthropic</code> instance. The type is auto-detected.</Field>
                <Field name="session" type="BrevitasSession"    defaultVal="None">Reuse a session to track multi-hop context across calls. A new one is created if omitted.</Field>
              </tbody>
            </table>
          </div>
        </Section>

        <Section id="sdk-cli" title="CLI">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            The <code className="font-mono text-brand-blue text-xs">pip install</code> puts a{' '}
            <code className="font-mono text-brand-blue text-xs">brevitas</code> command on your PATH.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <FieldHead />
              <tbody>
                <Field name="brevitas init"        type="cmd" defaultVal="—">Find your LLM call sites, wire Brevitas in, and start saving.</Field>
                <Field name="brevitas scan [path]"  type="cmd" defaultVal="—">List every LLM API call in a codebase. Add <code className="font-mono text-xs">--json</code> for machine output.</Field>
                <Field name="brevitas apply [path]" type="cmd" defaultVal="—">Rewrite detected clients to use <code className="font-mono text-xs">brevitas.wrap()</code>. Dry-run unless <code className="font-mono text-xs">-w/--write</code>.</Field>
                <Field name="brevitas start"        type="cmd" defaultVal="—">Start the local zero-code proxy.</Field>
                <Field name="brevitas config KEY VALUE" type="cmd" defaultVal="—">Set <code className="font-mono text-xs">api-key</code> or <code className="font-mono text-xs">base-url</code> locally.</Field>
                <Field name="brevitas status"       type="cmd" defaultVal="—">Check connectivity to the Brevitas API.</Field>
              </tbody>
            </table>
          </div>
        </Section>

        <Section id="sdk-multiturn" title="Multi-turn sessions">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            A wrapped client is stateful across calls — pass a shared{' '}
            <code className="font-mono text-brand-blue text-xs">BrevitasSession</code> so context from
            earlier hops is tracked and pruned for later ones.
          </p>
          <CodeBlock lang="python" code={`import openai
from brevitas import wrap, configure, BrevitasSession

configure(api_key="bvt_your_key_here")
session = BrevitasSession()
client = wrap(openai.OpenAI(), session=session)

# reuse the same client across turns
client.chat.completions.create(model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Design the database schema"}])
client.chat.completions.create(model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Now implement the API endpoints"}])
# each turn reuses compressed context from the previous turns`} />
        </Section>

        <Section id="sdk-frameworks" title="Frameworks (LangChain, CrewAI, …)">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Frameworks build their own provider clients internally, so the cleanest integration is the
            zero-code proxy — start it, point the base URL at it, and every framework call routes through
            Brevitas with no code change.
          </p>
          <CodeBlock lang="bash" code={`brevitas start --api-key bvt_your_key_here
export OPENAI_BASE_URL=http://localhost:4242/openai
# LangChain / CrewAI / LlamaIndex now route through Brevitas`} />
        </Section>
      </div>
    </div>
  )
}

function RestAPIDocs() {
  const BASE = 'https://brevitassystems.com'

  return (
    <div className="flex gap-12">
      <aside className="hidden lg:block w-44 shrink-0">
        <div className="sticky top-24 space-y-1">
          <p className="annotation tracking-widest uppercase mb-3">On this page</p>
          {API_NAV.map(n => (
            <a key={n.id} href={`#${n.id}`}
              className="block text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors py-0.5">
              {n.label}
            </a>
          ))}
        </div>
      </aside>

      <div className="flex-1 space-y-16 min-w-0">
        <div>
          <p className="annotation tracking-widest uppercase mb-4">REST API</p>
          <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight mb-4">
            Brevitas API
          </h2>
          <p className="text-brand-muted dark:text-brand-dark-muted text-base leading-relaxed max-w-xl">
            The same optimization engine over HTTP — language-agnostic and ready for server-side use.
            Authenticate all requests with your API key in the <code className="font-mono text-brand-blue text-sm">X-Brevitas-Key</code> header.
          </p>
          <p className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted mt-4">
            Base URL: <span className="text-brand-navy dark:text-brand-dark-navy">{BASE}</span>
          </p>
        </div>

        <Section id="api-overview" title="Overview">
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
        </Section>

        <Section id="api-auth" title="Authentication">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            All endpoints except <code className="font-mono text-brand-blue text-xs">/v1/health</code> and{' '}
            <code className="font-mono text-brand-blue text-xs">/v1/providers</code> require a Brevitas API key
            in the <code className="font-mono text-brand-blue text-xs">X-Brevitas-Key</code> header.
          </p>
          <CodeBlock lang="bash" code={`curl https://brevitassystems.com/v1/health \\
  -H "X-Brevitas-Key: bvt_your_key_here"`} />
        </Section>

        <Section id="api-compress" title="POST /v1/compress">
          <Endpoint method="POST" path="/v1/compress" description="compress + prune agent context" />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Runs the full compression pipeline and returns optimised text plus real token savings.
            Rate limited to <strong className="text-brand-navy dark:text-brand-dark-navy">60 requests/minute</strong> per key.
          </p>

          <p className="annotation mt-4 mb-2">// request body</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <FieldHead />
              <tbody>
                <Field name="messages"          type="string[]" defaultVal="required">Agent outputs to compress. Max 100 items, 50k chars each.</Field>
                <Field name="prior_context"     type="string[]" defaultVal="[]">Context chunks to prune. Max 200 items, 50k chars each.</Field>
                <Field name="task"              type="string"   defaultVal='""'>Current task description — used to score context relevance. Max 2,000 chars.</Field>
                <Field name="complexity"        type="float"    defaultVal="0.5">Task complexity 0–1. Higher = more context kept.</Field>
                <Field name="urgency"           type="float"    defaultVal="0.5">Urgency 0–1. Higher = favour recency in pruning.</Field>
                <Field name="compression_level" type="int"      defaultVal="2">1 light · 2 medium · 3 aggressive.</Field>
                <Field name="prune_budget"      type="int"      defaultVal="5">Max context chunks to keep (1–50).</Field>
                <Field name="delta_mode"        type="string"   defaultVal='"off"'><code className="font-mono text-xs">"on"</code> or <code className="font-mono text-xs">"off"</code>. Sends delta patches after turn 1.</Field>
                <Field name="wire_mode"         type="string"   defaultVal='"json"'><code className="font-mono text-xs">"json"</code> or <code className="font-mono text-xs">"msgpack"</code>.</Field>
              </tbody>
            </table>
          </div>

          <p className="annotation mt-6 mb-2">// response</p>
          <CodeBlock lang="json" code={`{
  "compressed_messages": ["..."],
  "pruned_context":      ["..."],
  "baseline_tokens":     412,
  "optimized_tokens":    171,
  "savings_pct":         58.5,
  "quality_proxy":       0.9921,
  "routed_model_hint":   "llama3.2",
  "model_response":      "...",
  "state_id":            "abc123"
}`} />

          <p className="annotation mt-4 mb-2">// example</p>
          <CodeBlock lang="bash" code={`curl -X POST https://brevitassystems.com/v1/compress \\
  -H "X-Brevitas-Key: bvt_your_key_here" \\
  -H "Content-Type: application/json" \\
  -d '{
    "messages": ["Agent A finished the plan. Here are the steps: ..."],
    "prior_context": ["User wants a FastAPI app", "Auth is JWT-based"],
    "task": "implement the endpoints",
    "complexity": 0.7
  }'`} />
        </Section>

        <Section id="api-stats" title="GET /v1/stats">
          <Endpoint method="GET" path="/v1/stats" description="usage statistics for the authenticated key · 120 req/min" />
          <CodeBlock lang="json" code={`{
  "total_calls":           142,
  "total_tokens_saved":    58210,
  "avg_savings_pct":       57.3,
  "avg_quality_proxy":     0.9918,
  "total_baseline_tokens": 98400,
  "total_optimized_tokens": 42190,
  "history": [
    {
      "timestamp":        "2026-06-13T18:42:00Z",
      "baseline_tokens":  412,
      "optimized_tokens": 171,
      "savings_pct":      58.5,
      "quality_proxy":    0.9921
    }
  ]
}`} />
        </Section>

        <Section id="api-provider" title="Provider config">
          <div className="space-y-6">
            <div className="space-y-2">
              <Endpoint method="PUT" path="/v1/provider" description="set model backend · 30 req/min" />
              <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
                <code className="font-mono text-brand-blue text-xs">provider_api_key</code> is not required for <code className="font-mono text-brand-blue text-xs">ollama</code>.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-left">
                  <FieldHead />
                  <tbody>
                    <Field name="provider"         type="string" defaultVal="required">
                      <code className="font-mono text-xs">ollama</code> · <code className="font-mono text-xs">anthropic</code> · <code className="font-mono text-xs">openai</code> · <code className="font-mono text-xs">grok</code> · <code className="font-mono text-xs">deepseek</code>
                    </Field>
                    <Field name="model"            type="string" defaultVal="required">Model name for the chosen provider.</Field>
                    <Field name="provider_api_key" type="string" defaultVal="—">Provider API key. Required for all except ollama.</Field>
                  </tbody>
                </table>
              </div>

              <div className="bg-brand-bg dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-4 text-xs">
                <p className="annotation mb-2">// supported providers &amp; models</p>
                <table className="w-full text-left">
                  <tbody>
                    {[
                      ['ollama',     'llama3.2, llama3.1, mistral, gemma3, phi4, qwen2.5'],
                      ['anthropic',  'claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5-20251001'],
                      ['openai',     'gpt-4o, gpt-4o-mini, o3-mini'],
                      ['grok',       'grok-3, grok-3-mini'],
                      ['deepseek',   'deepseek-chat, deepseek-reasoner'],
                    ].map(([p, m]) => (
                      <tr key={p} className="border-t border-brand-border dark:border-brand-dark-border">
                        <td className="py-2 pr-6 font-mono text-brand-blue">{p}</td>
                        <td className="py-2 font-mono text-brand-muted dark:text-brand-dark-muted">{m}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <CodeBlock lang="bash" code={`curl -X PUT https://brevitassystems.com/v1/provider \\
  -H "X-Brevitas-Key: bvt_your_key_here" \\
  -H "Content-Type: application/json" \\
  -d '{"provider": "openai", "provider_api_key": "sk-...", "model": "gpt-4o-mini"}'`} />
            </div>

            <div className="space-y-2">
              <Endpoint method="GET" path="/v1/provider" description="get current model backend — provider API key is masked" />
            </div>

            <div className="space-y-2">
              <Endpoint method="GET" path="/v1/providers" description="list all supported providers and models — no auth required" />
            </div>
          </div>
        </Section>

        <Section id="api-health" title="Health &amp; errors">
          <div className="space-y-4">
            <div className="space-y-2">
              <Endpoint method="GET" path="/v1/health" description="uptime check — no auth required" />
              <CodeBlock lang="json" code={`{ "status": "ok" }`} />
            </div>

            <p className="annotation mt-4 mb-2">// error responses</p>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <tbody>
                  {[
                    ['401', 'Missing or invalid X-Brevitas-Key'],
                    ['400', 'Validation error (see detail field)'],
                    ['413', 'Request body exceeds 2 MB'],
                    ['429', 'Rate limit exceeded'],
                  ].map(([status, meaning]) => (
                    <tr key={status} className="border-t border-brand-border dark:border-brand-dark-border">
                      <td className="py-2.5 pr-6 font-mono text-brand-blue">{status}</td>
                      <td className="py-2.5 text-brand-navy-mid dark:text-brand-dark-navy-mid">{meaning}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </Section>

        <Section id="api-example" title="End-to-end example">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Wrap an Anthropic client with the SDK, make a call, then read cumulative savings over the REST API.
          </p>
          <CodeBlock lang="python" code={`import os, requests
import anthropic, brevitas

BREVITAS_KEY = os.environ["BREVITAS_API_KEY"]

# 1. Point the SDK at Brevitas
brevitas.configure(
    api_key=BREVITAS_KEY,
    base_url="https://brevitassystems.com",
)

# 2. Wrap your existing provider client — nothing else changes
client = brevitas.wrap(anthropic.Anthropic())   # reads ANTHROPIC_API_KEY

# 3. Call as usual; the request is compressed + tracked automatically
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Build a rate-limited REST API with JWT auth"}],
)
print(resp.content[0].text)

# 4. Check cumulative usage over the REST API
stats = requests.get(
    "https://brevitassystems.com/v1/stats",
    headers={"X-Brevitas-Key": BREVITAS_KEY},
).json()
print(f"Total tokens saved: {stats['total_tokens_saved']:,}")`} />
        </Section>
      </div>
    </div>
  )
}

const PAGES = [
  { id: 'sdk', label: 'Python SDK' },
  { id: 'api', label: 'REST API' },
]

export default function Docs() {
  const [page, setPage] = useState('sdk')

  return (
    <div className="space-y-8">
      <div className="flex gap-1 border-b border-brand-border dark:border-brand-dark-border">
        {PAGES.map(p => (
          <button
            key={p.id}
            onClick={() => setPage(p.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              page === p.id
                ? 'border-brand-blue text-brand-navy dark:text-brand-dark-navy'
                : 'border-transparent text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy'
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {page === 'sdk' ? <PythonSDKDocs /> : <RestAPIDocs />}
    </div>
  )
}
