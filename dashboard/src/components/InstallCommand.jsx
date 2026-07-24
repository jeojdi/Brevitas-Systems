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

export default function InstallCommand({ phase = 'all', audience = 'personal' }) {
  const [activePlatform, setActivePlatform] = useState(BVX_PLATFORMS[0].id)
  const platform = BVX_PLATFORMS.find(item => item.id === activePlatform) || BVX_PLATFORMS[0]
  const showSetup = phase === 'all' || phase === 'setup'
  const showVerification = phase === 'all' || phase === 'verify'

  return (
    <div className="space-y-5 rounded-2xl border border-brand-border bg-white p-5 dark:border-brand-dark-border dark:bg-brand-dark-surface sm:p-6">
      {showSetup && (
        <section aria-labelledby="bvx-setup-heading" className="space-y-4">
          <div>
            <p className="annotation uppercase tracking-widest">// install, authenticate, configure</p>
            <h2 id="bvx-setup-heading" className="mt-1 font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              One command connects your tools
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Choose your operating system, copy the command, and follow the prompts. BVX opens this dashboard for
              authorization, stores a revocable device key, configures detected tools, starts local services, and checks the setup.
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
