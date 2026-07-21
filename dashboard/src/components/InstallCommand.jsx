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

export default function InstallCommand({ phase = 'all' }) {
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
              Connect a local AI tool with BVX
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">
              Choose your operating system. The setup command opens the dashboard for authorization, stores a revocable
              device key, configures detected tools, starts the local services, and runs setup checks.
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

          <CommandRow label={`1. Install BVX on ${platform.label}`} prompt={platform.prompt} command={platform.installCommand} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">{platform.note}</p>
          <CommandRow label="2. Confirm the installed binary" prompt={platform.prompt} command={BVX_COMMANDS.version} />
          <CommandRow label="3. Authorize, configure tools, and start services" prompt={platform.prompt} command={BVX_COMMANDS.setup} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            <code className="font-mono text-brand-blue">bvx install</code> includes browser authentication. Use{' '}
            <code className="font-mono text-brand-blue">bvx login</code> separately only when you need to reconnect the stored account.
          </p>
        </section>
      )}

      {showSetup && showVerification && <div className="h-px bg-brand-border dark:bg-brand-dark-border" />}

      {showVerification && (
        <section aria-labelledby="bvx-verify-heading" className="space-y-4">
          <div>
            <p className="annotation uppercase tracking-widest">// diagnose, then prove one request</p>
            <h2 id="bvx-verify-heading" className="mt-1 font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">
              Verify the complete local path
            </h2>
          </div>
          <CommandRow label="1. Require all installation diagnostics to pass" command={BVX_COMMANDS.diagnose} />
          <ol className="space-y-2 text-sm leading-relaxed text-brand-navy-mid dark:text-brand-dark-navy-mid">
            <li><span className="mr-2 font-mono text-brand-blue">2.</span>Send one ordinary prompt from a tool that BVX reported as configured.</li>
            <li><span className="mr-2 font-mono text-brand-blue">3.</span>Check the local, content-free proxy counters:</li>
          </ol>
          <CommandRow command={BVX_COMMANDS.verifyRequest} />
          <p className="text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">
            The first request is verified only when <span className="font-medium text-brand-navy dark:text-brand-dark-navy">Requests proxied</span>{' '}
            increases. A successful login or a healthy service alone does not prove that your AI tool is routed through BVX.
          </p>
        </section>
      )}
    </div>
  )
}
