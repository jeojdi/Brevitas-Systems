import { useState, useEffect } from 'react'
import { supabase, supabaseMisconfigured, getOrCreateApiKey } from './lib/supabase.js'
import Auth from './components/Auth.jsx'
import Overview from './components/Overview.jsx'
import Playground from './components/Playground.jsx'
import ModelConfig from './components/ModelConfig.jsx'
import Docs from './components/Docs.jsx'
import Billing from './components/Billing.jsx'
import Projects from './components/Projects.jsx'
import Admin from './components/Admin.jsx'
import ApiKeys from './components/ApiKeys.jsx'
import DeviceConnect from './components/DeviceConnect.jsx'

const BASE_TABS = ['Overview', 'Repositories', 'API Keys', 'Playground', 'Model', 'Docs', 'Billing']
const LIVE_REFRESH_MS = 10_000

function pendingDeviceCode() {
  const match = window.location.hash.match(/^#bvx=([A-Za-z0-9_-]{40,128})$/)
  if (match) {
    sessionStorage.setItem('bvx_device_code', match[1])
    history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
  }
  return sessionStorage.getItem('bvx_device_code') || ''
}

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
  const [session, setSession]     = useState(null)
  const [apiKey, setApiKey]       = useState('')
  const [keyLoading, setKeyLoading] = useState(false)
  const [keyError, setKeyError]   = useState('')
  const [authLoading, setAuthLoading] = useState(true)
  const [recoveringPassword, setRecoveringPassword] = useState(false)
  const [activeTab, setActiveTab] = useState('Overview')
  const [darkMode, setDarkMode]   = useState(() => localStorage.getItem('bvt_dark') === 'true')
  const [isAdmin, setIsAdmin]     = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)
  const [deviceCode, setDeviceCode] = useState(pendingDeviceCode)

  const toggleDark = () => {
    const next = !darkMode
    setDarkMode(next)
    localStorage.setItem('bvt_dark', String(next))
  }

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode)
  }, [darkMode])

  // Initialise Supabase session
  useEffect(() => {
    if (supabaseMisconfigured) { setAuthLoading(false); return }

    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session)
      setAuthLoading(false)
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      setSession(session)
      if (event === 'PASSWORD_RECOVERY') setRecoveringPassword(true)
      if (!session) setApiKey('')
    })

    return () => subscription.unsubscribe()
  }, [])

  // When a session exists, fetch or create the user's Brevitas API key
  useEffect(() => {
    if (!session) return
    setKeyLoading(true)
    setKeyError('')
    getOrCreateApiKey(session.user.id, session.access_token)
      .then(key => setApiKey(key))
      .catch(err => setKeyError(err.message))
      .finally(() => setKeyLoading(false))
  }, [session?.user?.id, session?.access_token])

  useEffect(() => {
    const metadata = session?.user?.app_metadata || {}
    setIsAdmin(metadata.brevitas_admin === true || metadata.role === 'brevitas_admin')
  }, [session?.user?.app_metadata])

  useEffect(() => {
    if (!apiKey) return
    const timer = window.setInterval(() => setRefreshTick(tick => tick + 1), LIVE_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [apiKey])

  const signOut = () => supabase.auth.signOut()

  if (supabaseMisconfigured) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center flex-col gap-3 px-6 text-center">
        <span className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">Configuration required</span>
        <p className="text-sm text-brand-muted dark:text-brand-dark-muted max-w-sm">
          Add <code className="font-mono text-xs bg-brand-blue-dim px-1 py-0.5 rounded">VITE_SUPABASE_URL</code> and{' '}
          <code className="font-mono text-xs bg-brand-blue-dim px-1 py-0.5 rounded">VITE_SUPABASE_ANON_KEY</code> to{' '}
          <code className="font-mono text-xs">dashboard/.env</code>, then rebuild.
        </p>
      </div>
    )
  }

  if (authLoading) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center">
        <span className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">
          Loading…
        </span>
      </div>
    )
  }

  if (recoveringPassword) {
    return <Auth darkMode={darkMode} onToggleDark={toggleDark} initialMode="recovery"
                 onPasswordUpdated={() => setRecoveringPassword(false)} />
  }

  if (!session) {
    return <Auth darkMode={darkMode} onToggleDark={toggleDark} />
  }

  if (deviceCode) {
    const done = () => {
      sessionStorage.removeItem('bvx_device_code')
      setDeviceCode('')
    }
    return <DeviceConnect accessToken={session.access_token} deviceCode={deviceCode}
                          email={session.user.email} onDone={done} />
  }

  if (keyLoading) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center">
        <span className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">
          Setting up your dashboard…
        </span>
      </div>
    )
  }

  if (keyError) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center flex-col gap-4">
        <p className="text-sm text-red-500">{keyError}</p>
        <button onClick={signOut} className="font-mono text-[11px] tracking-widest uppercase text-brand-muted hover:text-brand-navy transition-colors">
          Sign out
        </button>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col">
      {/* ── Floating pill nav ── */}
      <div className="sticky top-0 z-50 px-6 pt-5 pb-3">
        <header className="bg-white dark:bg-brand-dark-surface rounded-2xl border border-brand-border dark:border-brand-dark-border shadow-sm px-6 py-3.5 flex items-center justify-between max-w-7xl mx-auto">
          {/* Logo */}
          <a href="/" className="shrink-0 no-underline" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-7 w-auto dark:hidden" />
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-7 w-auto hidden dark:block" />
          </a>

          {/* Tabs */}
          <nav className="flex items-center gap-1">
            {[...BASE_TABS, ...(isAdmin ? ['Admin'] : [])].map(tab => (
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

          {/* Right: dark toggle + user email + sign out */}
          <div className="flex items-center gap-3 shrink-0">
            <span className="annotation hidden lg:flex items-center gap-1.5" title="Tracking runs server-side, even when this dashboard is closed">
              <span className="w-1.5 h-1.5 rounded-full bg-brand-teal" /> tracking active
            </span>
            <button
              onClick={toggleDark}
              className="text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
              title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {darkMode ? <SunIcon /> : <MoonIcon />}
            </button>
            <span className="text-[11px] text-brand-muted dark:text-brand-dark-muted hidden sm:block">
              {session.user.email}
            </span>
            <button
              onClick={signOut}
              className="text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
            >
              Sign out
            </button>
          </div>
        </header>
      </div>

      {/* ── Page content ── */}
      <main className="flex-1 px-6 pt-6 pb-16 max-w-7xl mx-auto w-full">
        {activeTab === 'Overview'   && <Overview     apiKey={apiKey} darkMode={darkMode} refreshTick={refreshTick} />}
        {activeTab === 'Repositories' && <Projects   apiKey={apiKey} refreshTick={refreshTick} />}
        {activeTab === 'API Keys'   && <ApiKeys      apiKey={apiKey} />}
        {activeTab === 'Playground' && <Playground   apiKey={apiKey} />}
        {activeTab === 'Model'      && <ModelConfig  apiKey={apiKey} />}
        {activeTab === 'Docs'       && <Docs />}
        {activeTab === 'Billing'    && <Billing apiKey={apiKey} refreshTick={refreshTick} />}
        {activeTab === 'Admin'      && <Admin accessToken={session.access_token} refreshTick={refreshTick} />}
      </main>
      <footer className="pb-8 flex justify-center gap-4 text-[11px] text-brand-muted dark:text-brand-dark-muted">
        <a href="/privacy" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Privacy</a>
        <a href="/terms" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Terms</a>
      </footer>
    </div>
  )
}
