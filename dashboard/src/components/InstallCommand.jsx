import { useState } from 'react'

// One-line install commands for the bvx CLI, OS-tabbed with copy-to-clipboard.
const COMMANDS = [
  { label: 'macOS',   prompt: '$', command: 'brew install brevitas-ai/brevitas/bvx && bvx login' },
  { label: 'Windows', prompt: '>', command: 'irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex' },
]

export default function InstallCommand() {
  const [active, setActive] = useState(0)
  const [copied, setCopied] = useState(false)
  const current = COMMANDS[active]

  const copy = () => {
    navigator.clipboard.writeText(current.command)
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }

  return (
    <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-5 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="annotation tracking-widest uppercase">// install the bvx CLI</p>
        <div className="flex gap-1.5">
          {COMMANDS.map((c, i) => (
            <button
              key={c.label}
              onClick={() => { setActive(i); setCopied(false) }}
              className={`font-mono text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                i === active
                  ? 'border-brand-blue bg-brand-blue text-white'
                  : 'border-brand-border dark:border-brand-dark-border text-brand-navy dark:text-brand-dark-navy hover:border-brand-blue'
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex items-center gap-3 bg-brand-bg dark:bg-brand-dark-bg border border-brand-border dark:border-brand-dark-border rounded-xl px-4 py-3">
        <span className="font-mono text-sm text-brand-blue select-none">{current.prompt}</span>
        <code className="font-mono text-xs sm:text-sm text-brand-navy dark:text-brand-dark-navy overflow-x-auto whitespace-nowrap flex-1">
          {current.command}
        </code>
        <button
          onClick={copy}
          className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors shrink-0"
        >
          {copied ? '✓ copied' : 'copy'}
        </button>
      </div>
    </div>
  )
}
