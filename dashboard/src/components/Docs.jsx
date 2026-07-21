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

const INSTALL_NAV = [
  { id: 'how-it-works',    label: 'How it works' },
  { id: 'requirements',    label: 'Requirements' },
  { id: 'install',         label: 'Install' },
  { id: 'setup',           label: 'First-time setup' },
  { id: 'verify',          label: 'Verify it works' },
  { id: 'service',         label: 'Background service' },
  { id: 'update',          label: 'Updating' },
  { id: 'uninstall',       label: 'Uninstalling' },
  { id: 'commands',        label: 'Command reference' },
  { id: 'troubleshooting', label: 'Troubleshooting' },
]

const PARTS = [
  ['bvx', 'The installer/manager CLI (written in Go). Detects your AI tools, stores one API key, points each tool at the local proxy, and runs the background service.', 'You — brew / install.ps1'],
  ['Proxy service', 'A local HTTP proxy on 127.0.0.1:8080 that every configured tool routes through. Runs in the background (bvx serve).', 'bvx — installs + supervises it'],
  ['brevitas-systems', 'The Python package holding the optimization logic. bvx talks to it over a local socket. Not bundled — installed and pinned via pip.', 'bvx install / update'],
]

const COMMANDS = [
  ['bvx install',   'Configure AI coding tools (install ai) or choose a codebase (install repo)'],
  ['bvx uninstall', 'Restore all tool configs and remove the background service'],
  ['bvx status',    'Show proxy, service, and provider status'],
  ['bvx stats',     'Show cumulative token-savings metrics from the proxy'],
  ['bvx providers', 'List supported providers and their detection/config state'],
  ['bvx doctor',    'Run diagnostics across the whole installation'],
  ['bvx repair',    'Re-apply configuration and restart the service'],
  ['bvx start / stop / restart', 'Control the background proxy service'],
  ['bvx logs',      'Print (or follow, with -f) the proxy logs'],
  ['bvx config',    'Print or edit Brevitas configuration'],
  ['bvx login / logout', 'Connect through the dashboard / remove the stored key'],
  ['bvx onboard',   'Scan a company backend and import existing customers safely'],
  ['bvx serve / optimizer', 'Run the proxy or optimization adapter in the foreground'],
  ['bvx update',    'Check for BVX and optimization-engine updates'],
  ['bvx version',   'Print version information'],
]

const TROUBLESHOOTING = [
  ['bvx: command not found (Windows)', 'Open a new terminal; PATH updates only apply to shells started after install.'],
  ['A tool still hits the provider directly', 'Run bvx status to confirm it was configured, then bvx repair to re-apply.'],
  ["Optimizer won't start", 'Make sure Python 3.13+ is installed and on your PATH, then run bvx update followed by bvx doctor.'],
  ['Anything else', 'bvx doctor inspects the whole installation and points at the specific problem.'],
]

export default function Docs() {
  return (
    <div className="flex gap-12">
      <aside className="hidden lg:block w-44 shrink-0">
        <div className="sticky top-24 space-y-1">
          <p className="annotation tracking-widest uppercase mb-3">On this page</p>
          {INSTALL_NAV.map(n => (
            <a key={n.id} href={`#${n.id}`}
              className="block text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors py-0.5">
              {n.label}
            </a>
          ))}
        </div>
      </aside>

      <div className="flex-1 space-y-16 min-w-0">
        <div>
          <p className="annotation tracking-widest uppercase mb-4">Install guide</p>
          <h2 className="font-serif text-4xl lg:text-5xl text-brand-navy dark:text-brand-dark-navy leading-tight mb-4">
            Installing Brevitas (bvx)
          </h2>
          <p className="text-brand-muted dark:text-brand-dark-muted text-base leading-relaxed max-w-xl">
            <code className="font-mono text-brand-blue text-sm">bvx</code> is the CLI that installs Brevitas,
            points each of your AI coding tools at a local token-trimming proxy, and supervises the background
            service. Install it, run <code className="font-mono text-brand-blue text-sm">bvx install</code> once,
            and you're set up on every supported platform.
          </p>
        </div>

        <Section id="how-it-works" title="How it works">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Brevitas is middleware that sits between your AI coding assistants and the LLM provider, trimming
            tokens on every request.
          </p>
          <div className="bg-brand-bg dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-xl p-5 overflow-x-auto">
            <p className="annotation mb-3">// request path</p>
            <pre className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid leading-relaxed whitespace-pre">{`AI Tool  ─▶  Brevitas Local Proxy  ─▶  brevitas-systems  ─▶  LLM Provider  ─▶  Response
             (127.0.0.1:8080)          (optimization,
                                         local socket)`}</pre>
          </div>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">There are three moving parts:</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Piece</th>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">What it is</th>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal">Who manages it</th>
                </tr>
              </thead>
              <tbody>
                {PARTS.map(([piece, what, who]) => (
                  <tr key={piece} className="border-t border-brand-border dark:border-brand-dark-border align-top">
                    <td className="py-2.5 pr-4 font-mono text-brand-blue whitespace-nowrap">{piece}</td>
                    <td className="py-2.5 pr-4 text-brand-navy-mid dark:text-brand-dark-navy-mid">{what}</td>
                    <td className="py-2.5 font-mono text-brand-muted dark:text-brand-dark-muted whitespace-nowrap">{who}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            <code className="font-mono text-brand-blue text-xs">bvx</code> never bundles the optimizer and never
            edits a tool config you haven't approved. Every config change is backed up before it's rewritten.
          </p>
        </Section>

        <Section id="requirements" title="Requirements">
          <ul className="space-y-3 text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed list-disc pl-5">
            <li><span className="text-brand-navy dark:text-brand-dark-navy font-medium">macOS, Linux, or Windows</span> (x86-64 or ARM64).</li>
            <li>
              <span className="text-brand-navy dark:text-brand-dark-navy font-medium">Python 3.13+</span> — required by{' '}
              <code className="font-mono text-brand-blue text-xs">brevitas-systems</code>. Homebrew installs it as a
              dependency automatically; on Windows install it yourself (e.g. from{' '}
              <a href="https://www.python.org/downloads/" className="text-brand-blue hover:underline">python.org</a> or{' '}
              <code className="font-mono text-brand-blue text-xs">winget install Python.Python.3.13</code>).
            </li>
            <li>
              An account at{' '}
              <a href="https://brevitassystems.com" className="text-brand-blue hover:underline">brevitassystems.com</a> —
              you authorize it during setup and the device key is stored in your OS credential store
              (Keychain / Credential Manager / Secret Service).
            </li>
          </ul>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            You do <span className="text-brand-navy dark:text-brand-dark-navy font-medium">not</span> need a Go toolchain
            or a C compiler — every install path below ships a prebuilt binary.
          </p>
        </Section>

        <Section id="install" title="Install">
          <p className="annotation mt-1 mb-1">// macOS (Homebrew)</p>
          <CodeBlock lang="bash" code={`brew tap Brevitas-ai/brevitas
brew install bvx`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">Or as a single command:</p>
          <CodeBlock lang="bash" code={`brew install Brevitas-ai/brevitas/bvx`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            To build the latest <code className="font-mono text-brand-blue text-xs">main</code> from source instead of a release binary:
          </p>
          <CodeBlock lang="bash" code={`brew install --HEAD Brevitas-ai/brevitas/bvx`} />

          <p className="annotation mt-4 mb-1">// Linux (Homebrew)</p>
          <CodeBlock lang="bash" code={`brew install Brevitas-ai/brevitas/bvx`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            The released Homebrew formula includes Linux x86-64 and ARM64 binaries and installs Python 3.13 as a dependency.
          </p>

          <p className="annotation mt-4 mb-1">// Windows (PowerShell)</p>
          <CodeBlock lang="powershell" code={`irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            This downloads the prebuilt <code className="font-mono text-brand-blue text-xs">bvx.exe</code> for your
            architecture, <span className="text-brand-navy dark:text-brand-dark-navy font-medium">verifies its SHA-256</span> against
            the release <code className="font-mono text-brand-blue text-xs">checksums.txt</code>, installs it to{' '}
            <code className="font-mono text-brand-blue text-xs">%LOCALAPPDATA%\Programs\bvx</code>, and adds that folder to
            your user PATH. Open a <span className="text-brand-navy dark:text-brand-dark-navy font-medium">new</span> terminal
            afterward so the updated PATH takes effect. To pin a version, set{' '}
            <code className="font-mono text-brand-blue text-xs">$env:BVX_VERSION</code> before running.
          </p>

          <p className="annotation mt-4 mb-1">// verify the binary is installed</p>
          <CodeBlock lang="bash" code={`bvx version`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            This only confirms the CLI is on your PATH — it does{' '}
            <span className="text-brand-navy dark:text-brand-dark-navy font-medium">not</span> configure anything yet.
            That's the next step.
          </p>
        </Section>

        <Section id="setup" title="First-time setup">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">Run the interactive installer once:</p>
          <CodeBlock lang="bash" code={`bvx install`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            This is the same as <code className="font-mono text-brand-blue text-xs">bvx install ai</code>. It scans your
            system for supported AI tools (Claude Code, Codex CLI, Continue, Aider, …), opens the Brevitas dashboard for
            one-click authorization, stores the device key in your OS credential store, rewrites each supported tool's
            config to route through <code className="font-mono text-brand-blue text-xs">http://127.0.0.1:8080</code>{' '}
            (backing up the original first), then installs, starts, and diagnoses the background services.
          </p>
          <CodeBlock lang="text" code={`Scanning system...

  ✓ Claude Code
  ✓ Codex CLI
  ✓ Continue
  ✓ Aider
  ⚠ Cursor (manual step required)
  ⚠ GitHub Copilot — Unsupported

Detected 4 configurable tool(s), 1 manual, 1 unsupported.

Opening https://brevitassystems.com/dashboard#bvx=...
Waiting for approval... approved

Installing...

  ✓ API key stored in macOS Keychain
  ✓ Claude Code configured`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            <span className="text-brand-navy dark:text-brand-dark-navy font-medium">Wiring up a codebase instead.</span> To
            route every LLM call in a project through Brevitas (rather than configuring interactive tools):
          </p>
          <CodeBlock lang="bash" code={`bvx install repo                 # choose a codebase, scan, and open its AI-call map
bvx install repo --apply         # also write a .env.agentmap you can source
bvx install repo --apply --auto  # also rewrite hardcoded provider URLs`} />
        </Section>

        <Section id="verify" title="Verify it works">
          <CodeBlock lang="bash" code={`bvx status     # proxy, service, and provider status
bvx doctor     # full diagnostics across the installation`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Require diagnostics to pass, then send one ordinary prompt from a tool BVX reported as configured. Prove that
            request used the proxy by checking its local counters:
          </p>
          <CodeBlock lang="bash" code={`bvx stats      # “Requests proxied” must increase after the prompt`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Login success and a healthy service do not prove an AI tool is routed through BVX. The request counter does.
          </p>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            If something looks off, re-apply config and restart the service:
          </p>
          <CodeBlock lang="bash" code={`bvx repair`} />
        </Section>

        <Section id="service" title="Background service">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">Control the background proxy service directly:</p>
          <CodeBlock lang="bash" code={`bvx start      # start the proxy service
bvx stop       # stop it
bvx restart    # restart it
bvx logs       # print the proxy logs
bvx logs -f    # follow the logs live`} />
        </Section>

        <Section id="update" title="Updating">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Upgrade the <code className="font-mono text-brand-blue text-xs">bvx</code> CLI itself with your package manager:
          </p>
          <CodeBlock lang="bash" code={`# macOS / Linux
brew upgrade bvx

# Windows — just re-run the installer; it fetches the latest release
irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Ask BVX to check both the CLI and compatible optimization engine:
          </p>
          <CodeBlock lang="bash" code={`bvx update`} />
        </Section>

        <Section id="uninstall" title="Uninstalling">
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            This restores every tool config from its backup and removes the background service:
          </p>
          <CodeBlock lang="bash" code={`bvx uninstall`} />
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">Then remove the CLI itself:</p>
          <CodeBlock lang="bash" code={`# macOS / Linux
brew uninstall bvx

# Windows
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\Programs\\bvx"
# and remove that folder from your user PATH (System Settings → Environment Variables)`} />
        </Section>

        <Section id="commands" title="Command reference">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Command</th>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal">Description</th>
                </tr>
              </thead>
              <tbody>
                {COMMANDS.map(([cmd, desc]) => (
                  <tr key={cmd} className="border-t border-brand-border dark:border-brand-dark-border align-top">
                    <td className="py-2.5 pr-4 font-mono text-brand-blue whitespace-nowrap">{cmd}</td>
                    <td className="py-2.5 text-brand-navy-mid dark:text-brand-dark-navy-mid">{desc}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            Run <code className="font-mono text-brand-blue text-xs">bvx help</code> to see the full list at any time.
          </p>
        </Section>

        <Section id="troubleshooting" title="Troubleshooting">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal pr-4">Symptom</th>
                  <th className="pb-2 text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted font-normal">Fix</th>
                </tr>
              </thead>
              <tbody>
                {TROUBLESHOOTING.map(([symptom, fix]) => (
                  <tr key={symptom} className="border-t border-brand-border dark:border-brand-dark-border align-top">
                    <td className="py-2.5 pr-4 font-mono text-brand-blue">{symptom}</td>
                    <td className="py-2.5 text-brand-navy-mid dark:text-brand-dark-navy-mid">{fix}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-sm text-brand-muted dark:text-brand-dark-muted leading-relaxed">
            For how the proxy and optimizer communicate under the hood, see{' '}
            <code className="font-mono text-brand-blue text-xs">PROTOCOL.md</code> in the repository.
          </p>
        </Section>
      </div>
    </div>
  )
}
