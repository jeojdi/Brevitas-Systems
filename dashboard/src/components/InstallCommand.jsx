import { useEffect, useRef, useState } from 'react'
import { BVX_COMMANDS, BVX_PLATFORMS } from '../lib/onboarding-cli.js'

function CommandRow({ label, prompt = '$', command }) {
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef(null)

  useEffect(() => () => window.clearTimeout(resetTimer.current), [])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
      window.clearTimeout(resetTimer.current)
      resetTimer.current = window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div>
      {label && <p className="mb-1.5 text-[11px] text-brand-muted dark:text-brand-dark-muted">{label}</p>}
      <div className="flex items-center gap-3 rounded-xl border border-brand-border bg-brand-bg px-4 py-3 dark:border-brand-dark-border dark:bg-brand-dark-bg">
        <span className="select-none font-mono text-sm text-brand-blue">{prompt}</span>
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs text-brand-navy sm:text-sm dark:text-brand-dark-navy">
          {command}
        </code>
        <button
          type="button"
          onClick={copy}
          aria-label={`Copy ${label || command}`}
          className="annotation shrink-0 transition-colors hover:text-brand-navy dark:hover:text-brand-dark-navy"
        >
          {copied ? '✓ copied' : 'copy'}
        </button>
      </div>
    </div>
  )
}

const HOSTED = Object.freeze({
  install: 'pip install brevitas-systems',
  key: 'brevitas connect',
  baseUrl: 'https://api.brevitassystems.com/v1',
})

// brevitas.hosted() sets the gateway base URL and the two headers for you, reading
// BREVITAS_API_KEY / BREVITAS_CUSTOMER_ID from the environment. A single-tenant key
// (what `brevitas connect` mints) needs no customer id at all.
const HOSTED_SNIPPET_WRAPPER = `from openai import OpenAI
import brevitas

# BREVITAS_API_KEY in your environment (brevitas connect writes it to .env).
client = brevitas.hosted(OpenAI())`

// Or configure the client by hand. X-Brevitas-Key is REQUIRED — it is the header the
// gateway authenticates on (api_key= stays your provider key, forwarded upstream).
const HOSTED_SNIPPET = `client = OpenAI(
    base_url="${HOSTED.baseUrl}",
    api_key=os.environ["OPENAI_API_KEY"],
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
    },
)`

// Claude called directly through Anthropic. Same header; the SDK differs.
const HOSTED_SNIPPET_ANTHROPIC = `client = Anthropic(
    base_url="${HOSTED.baseUrl}",
    api_key=os.environ["ANTHROPIC_API_KEY"],
    default_headers={"X-Brevitas-Key": os.environ["BREVITAS_API_KEY"]},
)`

// Claude Code and anything else that is environment-variables-only.
const HOSTED_SNIPPET_ENV = `export ANTHROPIC_BASE_URL="${HOSTED.baseUrl}"
export ANTHROPIC_CUSTOM_HEADERS="X-Brevitas-Key: \${BREVITAS_API_KEY}"`

export default function InstallCommand({ phase = 'all', audience = 'personal' }) {
  const [activePlatform, setActivePlatform] = useState(BVX_PLATFORMS[0].id)
  const platform = BVX_PLATFORMS.find(item => item.id === activePlatform) || BVX_PLATFORMS[0]
  const showSetup = phase === 'all' || phase === 'setup'
  const showVerification = phase === 'all' || phase === 'verify'

  return (
    <div className="space-y-5 rounded-2xl border border-brand-border bg-white p-5 dark:border-brand-dark-border dark:bg-brand-dark-surface sm:p-6">
      {showSetup && (
        <section aria-labelledby="connect-setup-heading" className="space-y-4">
          <div>
            <h2 id="connect-setup-heading" className="font-sans text-2xl font-semibold text-brand-navy dark:text-brand-dark-navy">
              Connect your tools to the gateway
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Install BVX and run one command. It routes your local AI tools (Claude Code, Cursor, Copilot, anything)
              through the hosted Brevitas gateway. No base URLs, no headers, no code changes, and usage is metered on the
              billable path.
            </p>
          </div>

          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Operating system">
            {BVX_PLATFORMS.map(item => (
              <button
                type="button"
                key={item.id}
                onClick={() => setActivePlatform(item.id)}
                aria-pressed={item.id === platform.id}
                className={`rounded-lg border px-3 py-2 font-mono text-xs transition-colors ${
                  item.id === platform.id
                    ? 'border-brand-blue bg-brand-blue text-white'
                    : 'border-brand-border text-brand-navy hover:border-brand-blue dark:border-brand-dark-border dark:text-brand-dark-navy'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>

          <CommandRow label="1. Install BVX" prompt={platform.prompt} command={platform.installCommand} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">{platform.note}</p>

          <CommandRow label="2. Connect to the gateway" prompt={platform.prompt} command={BVX_COMMANDS.connect} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            Approve the browser prompt. BVX signs you in, points every detected tool at the gateway, and attaches your key
            on each request, so your tools and your code stay exactly as they are.
          </p>

          <div className="rounded-xl border border-brand-blue/30 bg-brand-blue-dim px-4 py-3 text-xs leading-relaxed text-brand-navy dark:text-brand-dark-navy">
            <p>
              Confirm any time with <code className="font-mono text-brand-blue">bvx status</code> (look for the{' '}
              <span className="font-medium">Gateway</span> line); <code className="font-mono text-brand-blue">bvx disconnect</code>{' '}
              routes providers directly again. BVX runs a small local forwarder, so keep it up with{' '}
              <code className="font-mono text-brand-blue">bvx start</code>. Usage is attributed to your workspace
              automatically. Nothing to configure per tool.
            </p>
          </div>

          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            Prefer to keep every request on your machine and skip the gateway? Run{' '}
            <code className="font-mono text-brand-blue">bvx install</code> instead of{' '}
            <code className="font-mono text-brand-blue">bvx connect</code>. It stays private, but not on the billable path.
          </p>

          <details className="rounded-xl border border-brand-border px-4 py-3 dark:border-brand-dark-border">
            <summary className="cursor-pointer text-xs font-medium text-brand-navy dark:text-brand-dark-navy">
              Calling the API from your own code instead?
            </summary>
            <div className="mt-4 space-y-4">
              <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
                No proxy needed. Point your SDK at the gateway base URL and add one header. Get a key with{' '}
                <code className="font-mono text-brand-blue">brevitas connect</code> (it writes{' '}
                <code className="font-mono text-brand-blue">BREVITAS_API_KEY</code> to <code className="font-mono">.env</code>),
                then keep your code otherwise unchanged.
              </p>
              <CommandRow label="Install the SDK, then get a key" command={`${HOSTED.install} && ${HOSTED.key}`} />
              {[
                ['Recommended · brevitas.hosted()', HOSTED_SNIPPET_WRAPPER],
                ['OpenAI by hand', HOSTED_SNIPPET],
                ['Anthropic (Claude) by hand', HOSTED_SNIPPET_ANTHROPIC],
                ['Environment only (Claude Code)', HOSTED_SNIPPET_ENV],
              ].map(([label, snippet]) => (
                <div key={label}>
                  <p className="mb-1 font-mono text-[10px] uppercase tracking-widest text-brand-muted dark:text-brand-dark-muted">
                    {label}
                  </p>
                  <pre className="overflow-x-auto rounded-xl border border-brand-border bg-brand-bg px-4 py-3 font-mono text-xs leading-relaxed text-brand-navy dark:border-brand-dark-border dark:bg-brand-dark-bg dark:text-brand-dark-navy">
                    <code>{snippet}</code>
                  </pre>
                </div>
              ))}
              <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
                Reselling to your own customers? Add{' '}
                <code className="font-mono text-brand-blue">X-Brevitas-Customer-ID</code> per request and mint the key with{' '}
                <code className="font-mono text-brand-blue">brevitas connect --multi-tenant</code>. A single-tenant key needs no header.
              </p>
            </div>
          </details>
        </section>
      )}

      {showSetup && showVerification && <div className="h-px bg-brand-border dark:bg-brand-dark-border" />}

      {showVerification && (
        <section aria-labelledby="bvx-verify-heading" className="space-y-4">
          <div>
            <h2 id="bvx-verify-heading" className="font-sans text-2xl font-semibold text-brand-navy dark:text-brand-dark-navy">
              Verify it works
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Send one ordinary prompt from any tool BVX configured, then return here. This page detects the request
              automatically. No special test call.
            </p>
          </div>
          <CommandRow label="Optional: check the local status first" command={BVX_COMMANDS.status} />
          <details className="rounded-xl border border-brand-border px-4 py-3 dark:border-brand-dark-border">
            <summary className="cursor-pointer text-xs font-medium text-brand-navy dark:text-brand-dark-navy">Want to verify in the terminal too?</summary>
            <div className="mt-4"><CommandRow command={BVX_COMMANDS.verifyRequest} /></div>
            <p className="mt-3 text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              <span className="font-medium text-brand-navy dark:text-brand-dark-navy">Requests proxied</span> must increase.
            </p>
          </details>
        </section>
      )}
    </div>
  )
}
