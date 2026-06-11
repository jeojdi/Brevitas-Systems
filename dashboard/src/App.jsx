import { useState, useEffect } from 'react'
import KeySetup from './components/KeySetup.jsx'
import Overview from './components/Overview.jsx'
import Playground from './components/Playground.jsx'
import ApiKeys from './components/ApiKeys.jsx'
import ModelConfig from './components/ModelConfig.jsx'
import Docs from './components/Docs.jsx'

const TABS = ['Overview', 'Playground', 'Model', 'Docs', 'API Keys']

function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  )
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5"/>
      <line x1="12" y1="1" x2="12" y2="3"/>
      <line x1="12" y1="21" x2="12" y2="23"/>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
      <line x1="1" y1="12" x2="3" y2="12"/>
      <line x1="21" y1="12" x2="23" y2="12"/>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
    </svg>
  )
}

export default function App() {
  const [apiKey, setApiKey]       = useState(() => localStorage.getItem('bvt_api_key') || '')
  const [activeTab, setActiveTab] = useState('Overview')
  const [darkMode, setDarkMode]   = useState(() => localStorage.getItem('bvt_dark') === 'true')

  const saveKey  = (key) => { localStorage.setItem('bvt_api_key', key); setApiKey(key) }
  const clearKey = () => { localStorage.removeItem('bvt_api_key'); setApiKey('') }

  const toggleDark = () => {
    const next = !darkMode
    setDarkMode(next)
    localStorage.setItem('bvt_dark', String(next))
  }

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode)
  }, [darkMode])

  if (!apiKey) return <KeySetup onSave={saveKey} darkMode={darkMode} onToggleDark={toggleDark} />

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col">
      {/* ── Floating pill nav ── */}
      <div className="sticky top-0 z-50 px-6 pt-5 pb-3">
        <header className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border shadow-sm px-6 py-3.5 flex items-center justify-between max-w-7xl mx-auto">
          {/* Logo */}
          <a href="/" className="flex items-center gap-2 shrink-0 no-underline">
            <span className="font-serif text-[1.35rem] font-medium text-brand-navy dark:text-brand-dark-navy leading-none">Brevitas</span>
            <span className="font-mono text-[9px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted leading-none pt-0.5">
              Systems
            </span>
          </a>

          {/* Tabs */}
          <nav className="flex items-center gap-1">
            {TABS.map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-2 rounded-xl text-[11px] tracking-widest uppercase font-medium transition-colors ${
                  activeTab === tab
                    ? 'bg-brand-blue-dim dark:bg-brand-dark-blue-dim text-brand-blue'
                    : 'text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy'
                }`}
              >
                {tab}
              </button>
            ))}
          </nav>

          {/* Right: dark toggle + change key */}
          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={toggleDark}
              className="text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
              title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {darkMode ? <SunIcon /> : <MoonIcon />}
            </button>
            <button
              onClick={clearKey}
              className="text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
            >
              Change key
            </button>
          </div>
        </header>
      </div>

      {/* ── Page content ── */}
      <main className="flex-1 px-6 pt-6 pb-16 max-w-7xl mx-auto w-full">
        {activeTab === 'Overview'   && <Overview     apiKey={apiKey} darkMode={darkMode} />}
        {activeTab === 'Playground' && <Playground   apiKey={apiKey} />}
        {activeTab === 'Model'      && <ModelConfig  apiKey={apiKey} />}
        {activeTab === 'Docs'       && <Docs         apiKey={apiKey} />}
        {activeTab === 'API Keys'   && <ApiKeys      apiKey={apiKey} />}
      </main>
    </div>
  )
}
