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
  connect: 'brevitas connect',
  check: 'brevitas billing-check',
  baseUrl: 'https://api.brevitassystems.com/v1',
})

// X-Brevitas-Key is REQUIRED and is the header the gateway actually reads
// (api/server.py:1731). It has NO fallback: `api_key=` becomes an Authorization
// bearer, and nothing maps that to x-brevitas-key, so a snippet carrying only
// the customer id returns `401 Missing X-Brevitas-Key header` on the very first
// request. This block used to omit it.
const HOSTED_SNIPPET = `client = OpenAI(
    base_url="${HOSTED.baseUrl}",
    api_key=os.environ["OPENAI_API_KEY"],
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "acme",
    },
)`

// Claude called directly through Anthropic. Same two headers; the SDK differs,
// and `/v1` belongs on a gateway base URL (it is wrong on BREVITAS_BASE_URL,
// where it produces /v1/v1 and a silent 404).
const HOSTED_SNIPPET_ANTHROPIC = `client = Anthropic(
    base_url="${HOSTED.baseUrl}",
    api_key=os.environ["ANTHROPIC_API_KEY"],
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "acme",
    },
)`

// Claude Code and anything else that is environment-variables-only.
const HOSTED_SNIPPET_ENV = `export ANTHROPIC_BASE_URL="${HOSTED.baseUrl}"
export ANTHROPIC_CUSTOM_HEADERS="X-Brevitas-Key: \${BREVITAS_API_KEY}
X-Brevitas-Customer-ID: acme"`

export default function InstallCommand({ phase = 'all', audience = 'personal' }) {
  const [activePlatform, setActivePlatform] = useState(BVX_PLATFORMS[0].id)
  const [showLocal, setShowLocal] = useState(false)
  const platform = BVX_PLATFORMS.find(item => item.id === activePlatform) || BVX_PLATFORMS[0]
  const showSetup = phase === 'all' || phase === 'setup'
  const showVerification = phase === 'all' || phase === 'verify'

  return (
    <div className="space-y-5 rounded-2xl border border-brand-border bg-white p-5 dark:border-brand-dark-border dark:bg-brand-dark-surface sm:p-6">
      {showSetup && (
        <section aria-labelledby="hosted-setup-heading" className="space-y-4">
          <div>
            <p className="annotation uppercase tracking-widest">// recommended · metered · billable</p>
            <h2 id="hosted-setup-heading" className="mt-1 font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              Connect to the hosted gateway
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              One command. It opens this dashboard for approval, then hands back an organization service key scoped to
              this workspace. Nothing to install on your servers and no background service — the only change in your app
              is the client base URL. This is the path whose savings are verified and billable.
            </p>
          </div>

          <CommandRow label="1. Install the CLI" command={HOSTED.install} />
          <CommandRow label="2. Approve in the browser and receive the key" command={HOSTED.connect} />

          <div>
            <p className="mb-1.5 text-[11px] text-brand-muted dark:text-brand-dark-muted">
              3. Point your client at the gateway
            </p>
            {[
              ['OpenAI-compatible', HOSTED_SNIPPET],
              ['Anthropic (Claude)', HOSTED_SNIPPET_ANTHROPIC],
              ['Environment only (Claude Code)', HOSTED_SNIPPET_ENV],
            ].map(([label, snippet]) => (
              <div key={label} className="mt-2 first:mt-0">
                <p className="mb-1 font-mono text-[10px] uppercase tracking-widest text-brand-muted dark:text-brand-dark-muted">
                  {label}
                </p>
                <pre className="overflow-x-auto rounded-xl border border-brand-border bg-brand-bg px-4 py-3 font-mono text-xs leading-relaxed text-brand-navy dark:border-brand-dark-border dark:bg-brand-dark-bg dark:text-brand-dark-navy">
                  <code>{snippet}</code>
                </pre>
              </div>
            ))}
          </div>

          <div className="rounded-xl border border-brand-blue/30 bg-brand-blue-dim px-4 py-3 text-xs leading-relaxed text-brand-navy dark:text-brand-dark-navy">
            <p className="font-medium">
              <code className="font-mono text-brand-blue">X-Brevitas-Customer-ID</code> is required on every hosted request.
            </p>
            <p className="mt-1.5 text-brand-muted dark:text-brand-dark-muted">
              An organization service key returns{' '}
              <code className="font-mono">400 Organization service proxy calls require X-Brevitas-Customer-ID</code>{' '}
              without it. This is the most common reason a first request fails. One key can route traffic for many end
              customers, and the header says which — attribution is exact and stable, never inferred.{' '}
              <code className="font-mono text-brand-blue">brevitas connect</code> pins your own id to the key it mints so
              a missing header resolves rather than fails; send it anyway. Reselling to your own customers? Use{' '}
              <code className="font-mono text-brand-blue">brevitas connect --multi-tenant</code>, which leaves the key
              unpinned so a missing header stays a hard 400.
            </p>
          </div>

          <CommandRow label="4. Confirm the traffic is actually metered" command={HOSTED.check} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            {audience === 'company'
              ? 'Manage, rotate, and revoke these keys under Team & keys. Billing stays off until Brevitas attests your commercial arrangement — billing-check shows you that state directly.'
              : 'Billing stays off until Brevitas attests your commercial arrangement — billing-check shows you that state directly.'}
          </p>
        </section>
      )}

      {showSetup && <div className="h-px bg-brand-border dark:bg-brand-dark-border" />}

      {showSetup && (
        <section aria-labelledby="bvx-setup-heading" className="space-y-4">
          <div>
            <p className="annotation uppercase tracking-widest">// alternative · privacy-first · not billable</p>
            <h2 id="bvx-setup-heading" className="mt-1 font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              One command connects your tools
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Prefer to keep every request byte on your own machine? BVX installs a local proxy instead. Choose your
              operating system, copy the command, and follow the prompts. BVX opens this dashboard for authorization,
              stores a revocable device key, configures detected tools, starts local services, and checks the setup.
            </p>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Honest tradeoff: receipts from the local proxy are recorded <span className="font-medium">non-authoritative</span>,
              because a proxy running on your machine cannot certify its own savings. You get the dashboard and the
              accounting; this path is <span className="font-medium">not</span> eligible for savings-based pricing. Use
              the hosted gateway above for that.
            </p>
            <button
              type="button"
              onClick={() => setShowLocal(value => !value)}
              aria-expanded={showLocal}
              className="mt-3 text-xs font-medium text-brand-blue"
            >
              {showLocal ? 'Hide local install' : 'Show local install commands'}
            </button>
          </div>
        </section>
      )}

      {showSetup && showLocal && (
        <section aria-label="Local proxy install" className="space-y-4">
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

          <CommandRow label={`Copy and run on ${platform.label}`} prompt={platform.prompt} command={platform.quickStartCommand} />
          <div className="rounded-xl border border-brand-teal/30 bg-brand-teal-dim px-4 py-3 text-xs leading-relaxed text-brand-teal dark:bg-brand-dark-teal-dim">
            {audience === 'company'
              ? 'This key belongs to this device only. Use Team & keys for production services and teammate access.'
              : 'No API key copying is required. Approve the browser prompt and BVX handles the local configuration.'}
          </div>
          <details className="rounded-xl border border-brand-border px-4 py-3 dark:border-brand-dark-border">
            <summary className="cursor-pointer text-xs font-medium text-brand-navy dark:text-brand-dark-navy">Show manual steps</summary>
            <div className="mt-4 space-y-4">
              <CommandRow label="1. Install BVX" prompt={platform.prompt} command={platform.installCommand} />
              <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">{platform.note}</p>
              <CommandRow label="2. Confirm the installed binary" prompt={platform.prompt} command={BVX_COMMANDS.version} />
              <CommandRow label="3. Authorize and configure tools" prompt={platform.prompt} command={BVX_COMMANDS.setup} />
            </div>
          </details>
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            <code className="font-mono text-brand-blue">bvx install</code> includes browser authentication. Use{' '}
            <code className="font-mono text-brand-blue">bvx login</code> only when you need to reconnect the stored account.
          </p>
        </section>
      )}

      {showSetup && showVerification && <div className="h-px bg-brand-border dark:bg-brand-dark-border" />}

      {showVerification && (
        <section aria-labelledby="bvx-verify-heading" className="space-y-4">
          <div>
            <p className="annotation uppercase tracking-widest">// one check, one normal prompt</p>
            <h2 id="bvx-verify-heading" className="mt-1 font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              Verify the complete path
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              On the hosted gateway, <code className="font-mono text-brand-blue">brevitas billing-check</code> is the
              whole check — it names every unmet condition and exits non-zero until your traffic is genuinely billable.
              The steps below verify a local BVX install.
            </p>
          </div>
          <CommandRow label="1. Run the automatic diagnostics" command={BVX_COMMANDS.diagnose} />
          <ol className="space-y-2 text-sm leading-relaxed text-brand-navy-mid dark:text-brand-dark-navy-mid">
            <li><span className="mr-2 font-mono text-brand-blue">2.</span>Send one ordinary prompt from a tool that BVX reported as configured.</li>
            <li><span className="mr-2 font-mono text-brand-blue">3.</span>Return to this page; it detects the request automatically.</li>
          </ol>
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
