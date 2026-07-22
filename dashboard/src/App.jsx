import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import { authModeForPath, cacheApiKey, clearSessionKeyCache, supabase, supabaseMisconfigured, getOrCreateApiKey, invalidateCachedApiKey, LOGIN_AUDIENCE, loginAudienceForPath } from './lib/supabase.js'
import { activateCompany, fetchCompanyContext, normalizeCompanyContext } from './lib/company-context.js'
import { configureApiAuthenticationRecovery } from './lib/api.js'
import { bootstrapWorkspace, completeOnboarding, fetchOnboardingStatus } from './lib/onboarding-api.js'
import Auth from './components/Auth.jsx'
import Overview from './components/Overview.jsx'
import Playground from './components/Playground.jsx'
import Docs from './components/Docs.jsx'
import Billing from './components/Billing.jsx'
import Projects from './components/Projects.jsx'
import Admin from './components/Admin.jsx'
import ApiKeys from './components/ApiKeys.jsx'
import DeviceConnect from './components/DeviceConnect.jsx'
import CompanyAdministration from './components/CompanyAdministration.jsx'
import OnboardingWorkspaceChoice from './components/OnboardingWorkspaceChoice.jsx'
import InstallCommand from './components/InstallCommand.jsx'
import InvitationAcceptance, { hasPendingCompanyInvitation } from './components/InvitationAcceptance.jsx'
import { capture, identify, resetAnalytics } from './lib/analytics.js'
import { WORKSPACE_TYPE } from './lib/onboarding-workspace.js'

const PERSONAL_TABS = ['Overview', 'Projects', 'Connect', 'Workspace', 'Playground', 'Docs', 'Savings']
const ENTERPRISE_TABS = ['Overview', 'Repositories', 'Connect', 'Team & keys', 'API Keys', 'Playground', 'Docs', 'Savings']
const LIVE_REFRESH_MS = 10_000
const PREVIEW_SECTION = new URLSearchParams(window.location.search).get('preview')
const PREVIEW_MODE = ['localhost', '127.0.0.1'].includes(window.location.hostname)
  && ['dashboard', 'billing', 'onboarding', 'onboarding-personal', 'onboarding-enterprise', 'personal', 'enterprise', 'invitation'].includes(PREVIEW_SECTION)
const PREVIEW_STATS = {
  total_calls: 128,
  total_tokens_saved: 84200,
  total_optimized_tokens: 60300,
  total_actual_cost_usd: 74.26,
  total_verified_savings_usd: 31.68,
  total_actual_tokens: 60300,
  unpriced_calls: 2,
  billing_by_week: [
    { week_start: '2026-07-13', calls: 82, tokens_saved: 52600, actual_cost_usd: 46.12, verified_savings_usd: 20.40 },
    { week_start: '2026-07-06', calls: 46, tokens_saved: 31600, actual_cost_usd: 28.14, verified_savings_usd: 11.28 },
  ],
  history: [
    [1180, 690, 'agent-platform'], [1320, 710, 'agent-platform'],
    [980, 520, 'support-bot'], [1540, 770, 'agent-platform'],
    [1240, 810, 'research-pipeline'], [1710, 790, 'research-pipeline'],
    [1080, 590, 'support-bot'], [1420, 680, 'agent-platform'],
    [1880, 840, 'research-pipeline'], [1360, 720, 'agent-platform'],
    [1120, 610, 'support-bot'], [1640, 760, 'research-pipeline'],
  ].map(([baseline_tokens, optimized_tokens, project], index) => ({
    timestamp: new Date(Date.UTC(2026, 6, 16, 12, index * 5)).toISOString(),
    baseline_tokens,
    optimized_tokens,
    savings_pct: Number((((baseline_tokens - optimized_tokens) / baseline_tokens) * 100).toFixed(1)),
    project,
  })).reverse(),
}
const PREVIEW_BILLING = {
  configured: true,
  subscription_status: 'active',
  billing_period: 'weekly',
  current_period_start: '2026-07-16T00:00:00.000Z',
  current_period_end: '2026-07-23T00:00:00.000Z',
  period_tracking_valid: true,
  last_invoice_status: 'paid',
  estimated_fee_usd: 7.92,
  reported_fee_usd: 7.92,
  weekly_safety_cap_usd: 100,
  has_customer: true,
  needs_review: 0,
  capped_entries: 0,
}
const emptyCompanyContext = (loading = false) => ({
  companies: [], activeCompanyId: '', selectedCompanyId: '', loading, error: '',
  needsOnboarding: false, workspaceCreated: false,
})

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

function WorkspaceStart({ enterprise, onNavigate }) {
  const cards = enterprise
    ? [
        ['1', 'Connect an admin tool', 'Authorize one revocable device key and prove a request reaches BVX.'],
        ['2', 'Invite the right people', 'Give members, company admins, and billing admins only the access they need.'],
        ['3', 'Create machine identities', 'Use separate scoped, expiring service keys for production—not a human session key.'],
      ]
    : [
        ['1', 'Connect once', 'The guided installer detects your local AI tools and configures the request path.'],
        ['2', 'Keep working normally', 'Use your existing tools; token savings and provider spend appear here automatically.'],
        ['3', 'Add a team later', 'Your projects and usage stay in place if you move to an enterprise workflow.'],
      ]

  return (
    <section className="overflow-hidden rounded-2xl border border-brand-border bg-white dark:border-brand-dark-border dark:bg-brand-dark-surface">
      <div className="grid gap-0 lg:grid-cols-[1.15fr_1fr]">
        <div className="p-6 sm:p-8">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-brand-blue-dim px-3 py-1 text-[10px] font-medium uppercase tracking-[0.18em] text-brand-blue dark:bg-brand-dark-blue-dim">
              {enterprise ? 'Enterprise workspace' : 'Personal workspace'}
            </span>
            <span className="text-xs text-brand-teal">Ready</span>
          </div>
          <h1 className="mt-5 max-w-xl font-serif text-4xl leading-tight text-brand-navy dark:text-brand-dark-navy sm:text-5xl">
            {enterprise ? 'One shared boundary for people and production.' : 'Your AI setup, without the admin overhead.'}
          </h1>
          <p className="mt-4 max-w-xl text-sm leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid sm:text-base">
            {enterprise
              ? 'Centralize repositories, roles, service credentials, usage, and billing while keeping every device and backend independently revocable.'
              : 'Connect a local tool, keep using it normally, and see verified savings in one private workspace.'}
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <button type="button" onClick={() => onNavigate('Connect')} className="min-h-11 rounded-xl bg-brand-blue px-5 py-3 text-sm font-medium text-white hover:opacity-90">
              {enterprise ? 'Connect first admin tool' : 'Connect my tool'}
            </button>
            {enterprise && (
              <button type="button" onClick={() => onNavigate('Team & keys')} className="min-h-11 rounded-xl border border-brand-border px-5 py-3 text-sm font-medium text-brand-navy dark:border-brand-dark-border dark:text-brand-dark-navy">
                Open team setup
              </button>
            )}
            {!enterprise && (
              <button type="button" onClick={() => onNavigate('Workspace')} className="min-h-11 rounded-xl border border-brand-border px-5 py-3 text-sm font-medium text-brand-navy dark:border-brand-dark-border dark:text-brand-dark-navy">
                Workspace settings
              </button>
            )}
          </div>
        </div>
        <ol className="divide-y divide-brand-border border-t border-brand-border bg-brand-bg/70 dark:divide-brand-dark-border dark:border-brand-dark-border dark:bg-brand-dark-bg/50 lg:border-l lg:border-t-0">
          {cards.map(([number, title, description]) => (
            <li key={number} className="flex gap-4 p-5 sm:p-6">
              <span className="font-mono text-sm text-brand-blue">{number}</span>
              <div>
                <h2 className="text-sm font-medium text-brand-navy dark:text-brand-dark-navy">{title}</h2>
                <p className="mt-1 text-xs leading-relaxed text-brand-muted dark:text-brand-dark-muted">{description}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}

function ConnectionPage({ enterprise }) {
  return (
    <div className="space-y-6">
      <header className="max-w-3xl">
        <p className="annotation uppercase tracking-widest">{enterprise ? 'Enterprise device connection' : 'Personal quick start'}</p>
        <h1 className="mt-2 font-serif text-4xl text-brand-navy dark:text-brand-dark-navy sm:text-5xl">
          {enterprise ? 'Connect an admin tool safely.' : 'Connect your first tool in one command.'}
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-navy-mid sm:text-base">
          {enterprise
            ? 'This creates a revocable device credential for local work. Production servers use scoped service keys from Team & keys.'
            : 'BVX opens this dashboard for approval, configures supported tools, starts its local service, and checks the installation.'}
        </p>
      </header>
      <InstallCommand phase="all" audience={enterprise ? 'company' : 'personal'} />
    </div>
  )
}

function DashboardPreview({ darkMode, onToggleDark }) {
  const billingPreview = PREVIEW_SECTION === 'billing'
  const onboardingType = PREVIEW_SECTION === 'onboarding-personal'
    ? WORKSPACE_TYPE.PERSONAL
    : PREVIEW_SECTION === 'onboarding-enterprise'
      ? WORKSPACE_TYPE.COMPANY
      : ''
  if (PREVIEW_SECTION === 'onboarding' || onboardingType) {
    return <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg px-4 py-10 sm:px-6 sm:py-16">
      <OnboardingWorkspaceChoice initialWorkspaceType={onboardingType} onContinue={async () => {}} />
    </div>
  }
  if (PREVIEW_SECTION === 'invitation') {
    return <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg">
      <InvitationAcceptance
        invitationToken={`bvi_${'x'.repeat(43)}`}
        accessToken="preview-authenticated-session"
        request={async () => Response.json({
          company_id: '11111111-1111-4111-8111-111111111111',
          role: 'member',
          status: 'accepted',
        })}
      />
    </div>
  }
  const enterprisePreview = PREVIEW_SECTION === 'enterprise'
  const personalPreview = PREVIEW_SECTION === 'personal'
  const previewTabs = enterprisePreview ? ENTERPRISE_TABS : personalPreview ? PERSONAL_TABS : ['Overview']
  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col">
      <div className="sticky top-0 z-50 px-2 sm:px-6 pt-2 sm:pt-5 pb-2 sm:pb-3">
        <header className="bg-white dark:bg-brand-dark-surface rounded-xl sm:rounded-2xl border border-brand-border dark:border-brand-dark-border shadow-sm max-w-7xl mx-auto overflow-hidden">
          <div className="px-3 sm:px-6 py-3 sm:py-4 flex items-center justify-between gap-3">
            <a href="/" className="shrink-0 no-underline" aria-label="Brevitas Systems home">
              <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-6 sm:h-7 w-auto dark:hidden" />
              <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-6 sm:h-7 w-auto hidden dark:block" />
            </a>
            <div className="flex items-center gap-2 sm:gap-4">
              <span className="annotation flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-brand-teal" /> local preview
              </span>
              <button
                onClick={onToggleDark}
                className="w-10 h-10 inline-flex items-center justify-center text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
                title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
                aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
              >
                {darkMode ? <SunIcon /> : <MoonIcon />}
              </button>
            </div>
          </div>
          <nav className="flex items-center gap-2 overflow-x-auto border-t border-brand-border px-2 py-2.5 dark:border-brand-dark-border sm:px-5 sm:py-3" aria-label="Dashboard preview section">
            {(billingPreview ? ['Savings'] : previewTabs).map((tab, index) => (
              <span key={tab} className={`inline-flex min-h-11 shrink-0 items-center rounded-xl px-4 py-2.5 text-[11px] font-medium uppercase tracking-widest ${index === 0 ? 'bg-brand-blue-dim text-brand-blue dark:bg-brand-dark-blue-dim' : 'text-brand-muted dark:text-brand-dark-muted'}`}>
                {tab}
              </span>
            ))}
          </nav>
        </header>
      </div>
      <main className="flex-1 min-w-0 px-3 sm:px-6 pt-6 sm:pt-8 pb-12 sm:pb-16 max-w-7xl mx-auto w-full">
        {enterprisePreview || personalPreview ? (
          <div className="space-y-10">
            <WorkspaceStart enterprise={enterprisePreview} onNavigate={() => {}} />
            <Overview apiKey="preview" darkMode={darkMode} refreshTick={0} previewStats={PREVIEW_STATS} showInstallCommand={false} />
          </div>
        ) : billingPreview
          ? <Billing apiKey="preview" accessToken="preview" refreshTick={0} previewStats={PREVIEW_STATS} previewBilling={PREVIEW_BILLING} />
          : <Overview apiKey="preview" darkMode={darkMode} refreshTick={0} previewStats={PREVIEW_STATS} />}
      </main>
    </div>
  )
}

export default function App() {
  const [loginAudience] = useState(() => loginAudienceForPath(window.location.pathname))
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
  const [companyContext, setCompanyContext] = useState(emptyCompanyContext)
  const [companyRefreshTick, setCompanyRefreshTick] = useState(0)
  const [companySwitching, setCompanySwitching] = useState(false)
  const [companySwitchError, setCompanySwitchError] = useState('')
  const [pendingCompanyInvitation, setPendingCompanyInvitation] = useState(hasPendingCompanyInvitation)
  const credentialUserId = useRef('')
  const credentialCompanyId = useRef('')

  const toggleDark = () => {
    const next = !darkMode
    setDarkMode(next)
    localStorage.setItem('bvt_dark', String(next))
  }

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode)
  }, [darkMode])

  useEffect(() => {
    if (session && loginAudience) history.replaceState(null, '', '/dashboard')
  }, [session, loginAudience])

  // Initialise Supabase session
  useEffect(() => {
    if (PREVIEW_MODE) { setAuthLoading(false); return }
    if (supabaseMisconfigured) { setAuthLoading(false); return }

    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session)
      setAuthLoading(false)
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      setSession(session)
      if (event === 'PASSWORD_RECOVERY') setRecoveringPassword(true)
      if (!session) {
        clearSessionKeyCache()
        credentialUserId.current = ''
        credentialCompanyId.current = ''
        setApiKey('')
        setCompanyContext(emptyCompanyContext())
      }
    })

    return () => subscription.unsubscribe()
  }, [])

  useEffect(() => {
    const nextUserId = session?.user?.id || ''
    const nextCompanyId = companyContext.activeCompanyId || ''
    const userChanged = !nextUserId || credentialUserId.current !== nextUserId
    const companyChanged = credentialCompanyId.current !== nextCompanyId
    if (userChanged || companyChanged) {
      clearSessionKeyCache()
      setApiKey('')
    }
    if (userChanged) {
      setCompanyContext(emptyCompanyContext())
    }
    credentialUserId.current = nextUserId
    credentialCompanyId.current = nextCompanyId
  }, [session?.user?.id, companyContext.activeCompanyId])

  useEffect(() => {
    if (!session?.user?.id || !session.access_token) return
    const controller = new AbortController()
    setCompanyContext(current => ({ ...current, loading: true, error: '' }))
    fetchCompanyContext(session.access_token, { signal: controller.signal })
      .then(context => setCompanyContext(current => ({
        ...context,
        selectedCompanyId: context.companies.some(
          company => company.company_id === current.selectedCompanyId)
          ? current.selectedCompanyId
          : context.activeCompanyId,
        loading: false,
        error: '',
        needsOnboarding: context.onboarding.status !== 'complete',
        workspaceCreated: true,
      })))
      .catch(reason => {
        if (reason?.name !== 'AbortError') {
          setCompanyContext(reason?.status === 403
            ? { ...emptyCompanyContext(), needsOnboarding: true }
            : { ...emptyCompanyContext(), error: reason?.message || 'Company access unavailable' })
        }
      })
    return () => controller.abort()
  }, [session?.user?.id, session?.access_token, apiKey, companyRefreshTick])

  const acceptCompanyCapabilities = useCallback(payload => {
    if (!session?.user?.id || credentialUserId.current !== session.user.id) return
    try {
      const context = normalizeCompanyContext(payload)
      setCompanyContext(current => ({
        ...context,
        selectedCompanyId: context.companies.some(
          company => company.company_id === current.selectedCompanyId)
          ? current.selectedCompanyId
          : context.activeCompanyId,
        loading: false,
        error: '',
        needsOnboarding: false,
        workspaceCreated: true,
      }))
    } catch {
      setCompanyContext({ ...emptyCompanyContext(), error: 'Company access unavailable' })
    }
  }, [session?.user?.id])

  const selectDeviceCompany = useCallback(companyId => {
    setCompanyContext(current => current.companies.some(
      company => company.company_id === companyId)
      ? { ...current, selectedCompanyId: companyId }
      : current)
  }, [])

  const setupWorkspace = useCallback(async selection => {
    if (!session?.access_token) throw new Error('Sign in again to create a workspace.')
    const workspace = await bootstrapWorkspace(session.access_token, selection)
    setCompanyContext(current => ({
      ...current,
      companies: [{
        company_id: workspace.company_id,
        company_name: workspace.company_name,
        role: workspace.role,
        account_type: workspace.account_type,
      }],
      activeCompanyId: workspace.company_id,
      selectedCompanyId: workspace.company_id,
      loading: false,
      error: '',
      needsOnboarding: true,
      workspaceCreated: true,
    }))
  }, [session?.access_token])

  const finishWorkspaceSetup = useCallback(async () => {
    if (!session?.access_token) throw new Error('Sign in again to verify onboarding.')
    await completeOnboarding(session.access_token)
    setCompanyContext({ ...emptyCompanyContext(true), needsOnboarding: false })
    setCompanyRefreshTick(value => value + 1)
  }, [session?.access_token])

  const checkWorkspaceSetup = useCallback(async () => {
    if (!session?.access_token) throw new Error('Sign in again to check onboarding.')
    return fetchOnboardingStatus(session.access_token)
  }, [session?.access_token])

  const switchCompany = useCallback(async companyId => {
    if (!session?.access_token || companyId === companyContext.activeCompanyId || companySwitching) return
    setCompanySwitching(true)
    setCompanySwitchError('')
    try {
      await activateCompany(session.access_token, companyId)
      credentialCompanyId.current = companyId
      clearSessionKeyCache()
      setApiKey('')
      setCompanyContext(current => ({
        ...current,
        activeCompanyId: companyId,
        selectedCompanyId: companyId,
        loading: true,
        error: '',
      }))
      setCompanyRefreshTick(value => value + 1)
    } catch (reason) {
      setCompanySwitchError(reason?.message || 'Could not switch company')
    } finally {
      setCompanySwitching(false)
    }
  }, [companyContext.activeCompanyId, companySwitching, session?.access_token])

  const acceptedCompanyInvitation = useCallback(result => {
    credentialCompanyId.current = ''
    setCompanyContext({ ...emptyCompanyContext(true), needsOnboarding: false })
    clearSessionKeyCache()
    setApiKey('')
    Promise.resolve()
      .then(() => activateCompany(session?.access_token || '', result?.company_id || ''))
      .then(() => setCompanyRefreshTick(value => value + 1))
      .catch(reason => setCompanyContext({
        ...emptyCompanyContext(),
        error: reason?.message || 'You joined the company, but it could not be selected.',
      }))
  }, [session?.access_token])

  useLayoutEffect(() => {
    const userId = session?.user?.id || ''
    const accessToken = session?.access_token || ''
    const companyId = companyContext.activeCompanyId || ''
    if (!userId || !accessToken || !companyId) {
      return configureApiAuthenticationRecovery(null)
    }
    return configureApiAuthenticationRecovery(async rejectedApiKey => {
      if (
        credentialUserId.current !== userId
        || credentialCompanyId.current !== companyId
      ) return ''
      invalidateCachedApiKey(userId, companyId, rejectedApiKey)
      const replacement = await getOrCreateApiKey(
        userId, accessToken, companyId,
      )
      if (
        credentialUserId.current !== userId
        || credentialCompanyId.current !== companyId
      ) return ''
      setApiKey(replacement)
      setKeyError('')
      return replacement
    })
  }, [session?.user?.id, session?.access_token, companyContext.activeCompanyId])

  // When a session exists, fetch or create this tab's company-scoped key.
  useEffect(() => {
    if (!session || pendingCompanyInvitation || companyContext.needsOnboarding || !companyContext.activeCompanyId || companyContext.loading || companySwitching) return
    let active = true
    const userId = session.user.id
    const companyId = companyContext.activeCompanyId
    setKeyLoading(true)
    setKeyError('')
    getOrCreateApiKey(userId, session.access_token, companyId)
      .then(key => {
        if (
          active
          && credentialUserId.current === userId
          && credentialCompanyId.current === companyId
        ) setApiKey(key)
      })
      .catch(err => { if (active) setKeyError(err.message) })
      .finally(() => { if (active) setKeyLoading(false) })
    return () => { active = false }
  }, [session?.user?.id, session?.access_token, pendingCompanyInvitation, companyContext.needsOnboarding, companyContext.activeCompanyId, companyContext.loading, companySwitching])

  useEffect(() => {
    const metadata = session?.user?.app_metadata || {}
    setIsAdmin(metadata.brevitas_admin === true || metadata.role === 'brevitas_admin')
  }, [session?.user?.app_metadata])

  useEffect(() => {
    if (!session?.user?.id) return
    identify(session.user.id, { email: session.user.email, account_type: isAdmin ? 'admin' : 'customer' })
  }, [session?.user?.id, session?.user?.email, isAdmin])

  useEffect(() => {
    if (!session) return
    capture('dashboard_tab_viewed', { tab: activeTab })
  }, [session, activeTab])

  useEffect(() => {
    if (!apiKey) return
    const timer = window.setInterval(() => setRefreshTick(tick => tick + 1), LIVE_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [apiKey])

  const signOut = () => {
    capture('account_signed_out')
    resetAnalytics()
    clearSessionKeyCache()
    credentialUserId.current = ''
    credentialCompanyId.current = ''
    setApiKey('')
    setCompanyContext(emptyCompanyContext())
    return supabase.auth.signOut()
  }
  const activateApiKey = async key => {
    setApiKey(key)
    try {
      await cacheApiKey(session.user.id, companyContext.activeCompanyId, key)
    } catch { /* active for this session; next login self-heals */ }
  }
  const activeWorkspace = companyContext.companies.find(
    company => company.company_id === companyContext.activeCompanyId,
  )
  const enterpriseWorkspace = activeWorkspace?.account_type === 'company'
  const dashboardTabs = enterpriseWorkspace ? ENTERPRISE_TABS : PERSONAL_TABS

  useEffect(() => {
    if (!dashboardTabs.includes(activeTab)) setActiveTab('Overview')
  }, [activeTab, dashboardTabs])

  if (PREVIEW_MODE) {
    return <DashboardPreview darkMode={darkMode} onToggleDark={toggleDark} />
  }

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
    return <Auth darkMode={darkMode} onToggleDark={toggleDark} initialMode={authModeForPath(window.location.pathname)} loginAudience={loginAudience} />
  }

  if (pendingCompanyInvitation) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg">
        <InvitationAcceptance
          accessToken={session.access_token}
          onAccepted={acceptedCompanyInvitation}
          onDismiss={() => setPendingCompanyInvitation(false)}
          onUseDifferentAccount={signOut}
        />
      </div>
    )
  }

  if (deviceCode && companyContext.activeCompanyId && !companyContext.loading) {
    const done = () => {
      sessionStorage.removeItem('bvx_device_code')
      setDeviceCode('')
    }
    return <DeviceConnect accessToken={session.access_token} deviceCode={deviceCode}
                          email={session.user.email} companies={companyContext.companies}
                          selectedCompanyId={companyContext.selectedCompanyId}
                          companyLoading={companyContext.loading}
                          companyError={companyContext.error}
                          onSelectCompany={selectDeviceCompany}
                          onRefreshCompanies={() => setCompanyRefreshTick(value => value + 1)}
                          onDone={done} />
  }

  if (companyContext.needsOnboarding) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg px-4 py-10 sm:px-6 sm:py-16">
        <div className="mb-8 flex items-center justify-between max-w-5xl mx-auto">
          <a href="/" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-8 w-auto dark:hidden" />
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-8 w-auto hidden dark:block" />
          </a>
          <div className="flex items-center gap-2">
            <button onClick={toggleDark} className="w-10 h-10 inline-flex items-center justify-center text-brand-muted dark:text-brand-dark-muted" aria-label="Toggle dark mode">
              {darkMode ? <SunIcon /> : <MoonIcon />}
            </button>
            <button onClick={signOut} className="min-h-10 px-2 text-[11px] text-brand-muted hover:text-brand-navy dark:text-brand-dark-muted dark:hover:text-brand-dark-navy">
              Sign out
            </button>
          </div>
        </div>
        <OnboardingWorkspaceChoice
          initialWorkspaceCreated={companyContext.workspaceCreated}
          initialWorkspaceType={activeWorkspace?.account_type === 'individual'
            ? WORKSPACE_TYPE.PERSONAL
            : activeWorkspace?.account_type === 'company'
              ? WORKSPACE_TYPE.COMPANY
              : loginAudience === LOGIN_AUDIENCE.PERSONAL
            ? WORKSPACE_TYPE.PERSONAL
            : loginAudience === LOGIN_AUDIENCE.ENTERPRISE
              ? WORKSPACE_TYPE.COMPANY
              : ''}
          onContinue={setupWorkspace}
          onCheck={checkWorkspaceSetup}
          onFinish={finishWorkspaceSetup}
        />
      </div>
    )
  }

  if (companyContext.error) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center flex-col gap-4 px-6 text-center">
        <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">Workspace access is unavailable.</p>
        <p className="text-sm text-red-500">{companyContext.error}</p>
        <div className="flex gap-4">
          <button onClick={() => setCompanyRefreshTick(value => value + 1)} className="font-mono text-[11px] tracking-widest uppercase text-brand-blue">Retry</button>
          <button onClick={signOut} className="font-mono text-[11px] tracking-widest uppercase text-brand-muted">Sign out</button>
        </div>
      </div>
    )
  }

  if (!companyContext.activeCompanyId || companyContext.loading || companySwitching) {
    return (
      <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex items-center justify-center">
        <span className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">
          {companySwitching ? 'Switching workspace…' : 'Loading your workspace…'}
        </span>
      </div>
    )
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
        <div className="flex gap-4">
          <button onClick={() => window.location.reload()} className="font-mono text-[11px] tracking-widest uppercase text-brand-blue hover:text-brand-navy transition-colors">
            Retry
          </button>
          <button onClick={signOut} className="font-mono text-[11px] tracking-widest uppercase text-brand-muted hover:text-brand-navy transition-colors">
            Sign out
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-brand-bg dark:bg-brand-dark-bg flex flex-col">
      {/* ── Dashboard header ── */}
      <div className="sticky top-0 z-50 px-2 sm:px-6 pt-2 sm:pt-5 pb-2 sm:pb-3">
        <header className="bg-white dark:bg-brand-dark-surface rounded-xl sm:rounded-2xl border border-brand-border dark:border-brand-dark-border shadow-sm max-w-7xl mx-auto overflow-hidden">
          <div className="px-3 sm:px-6 py-3 sm:py-4 flex items-center justify-between gap-3 sm:gap-5">
            <a href="/" className="shrink-0 no-underline" aria-label="Brevitas Systems home">
              <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-6 sm:h-7 w-auto dark:hidden" />
              <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-6 sm:h-7 w-auto hidden dark:block" />
            </a>

            {/* Right: dark toggle + user email + sign out */}
            <div className="flex items-center gap-1 sm:gap-4 shrink-0">
              <span className="annotation hidden lg:flex items-center gap-1.5" title="Tracking runs server-side, even when this dashboard is closed">
                <span className="w-1.5 h-1.5 rounded-full bg-brand-teal" /> tracking active
              </span>
              <button
                onClick={toggleDark}
                className="w-10 h-10 inline-flex items-center justify-center text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
                title={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
              >
                {darkMode ? <SunIcon /> : <MoonIcon />}
              </button>
              {companyContext.companies.length > 1 ? (
                <label className="block">
                  <span className="sr-only">Active workspace</span>
                  <select
                    value={companyContext.activeCompanyId}
                    onChange={event => switchCompany(event.target.value)}
                    disabled={companySwitching}
                    className="max-w-36 rounded-lg border border-brand-border bg-brand-bg px-2 py-2 text-[11px] text-brand-navy dark:border-brand-dark-border dark:bg-brand-dark-bg dark:text-brand-dark-navy sm:max-w-52"
                  >
                    {companyContext.companies.map(company => (
                      <option key={company.company_id} value={company.company_id}>{company.company_name}</option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="hidden max-w-52 items-center gap-2 md:flex">
                  <span className="rounded-full bg-brand-blue-dim px-2 py-1 text-[9px] font-medium uppercase tracking-wider text-brand-blue dark:bg-brand-dark-blue-dim">
                    {enterpriseWorkspace ? 'Enterprise' : 'Personal'}
                  </span>
                  <span className="truncate text-[11px] text-brand-muted dark:text-brand-dark-muted">
                    {companyContext.companies[0]?.company_name}
                  </span>
                </div>
              )}
              <span data-ph-sensitive className="text-[11px] text-brand-muted dark:text-brand-dark-muted hidden sm:block">
                {session.user.email}
              </span>
              <button
                onClick={signOut}
                className="min-h-10 px-2 text-[11px] text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors tracking-wide"
              >
                Sign out
              </button>
            </div>
          </div>

          <nav
            className="border-t border-brand-border dark:border-brand-dark-border px-2 sm:px-5 py-2.5 sm:py-3 flex items-center gap-2 overflow-x-auto"
            aria-label="Dashboard sections"
          >
            {[...dashboardTabs, ...(isAdmin ? ['Admin'] : [])].map(tab => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                aria-current={activeTab === tab ? 'page' : undefined}
                className={`shrink-0 min-h-11 px-4 py-2.5 rounded-xl text-[11px] tracking-widest uppercase font-medium transition-colors ${
                  activeTab === tab
                    ? 'bg-brand-blue-dim dark:bg-brand-dark-blue-dim text-brand-blue'
                    : 'text-brand-muted dark:text-brand-dark-muted hover:text-brand-navy dark:hover:text-brand-dark-navy hover:bg-brand-bg dark:hover:bg-brand-dark-elevated'
                }`}
              >
                {tab}
              </button>
            ))}
          </nav>
        </header>
      </div>

      {/* ── Page content ── */}
      <main className="flex-1 min-w-0 px-3 sm:px-6 pt-6 sm:pt-8 pb-12 sm:pb-16 max-w-7xl mx-auto w-full">
        {companySwitchError && <div role="alert" className="mb-5 flex items-center justify-between gap-3 rounded-xl border border-red-200 px-4 py-3 text-xs text-red-500 dark:border-red-900/40">
          <span>{companySwitchError}</span>
          <button type="button" onClick={() => setCompanySwitchError('')} className="font-mono uppercase tracking-wider">Dismiss</button>
        </div>}
        {activeTab === 'Overview'   && <div className="space-y-10"><WorkspaceStart enterprise={enterpriseWorkspace} onNavigate={setActiveTab} /><Overview apiKey={apiKey} darkMode={darkMode} refreshTick={refreshTick} showInstallCommand={false} /></div>}
        {(activeTab === 'Repositories' || activeTab === 'Projects') && <Projects apiKey={apiKey} refreshTick={refreshTick} />}
        {activeTab === 'Connect' && <ConnectionPage enterprise={enterpriseWorkspace} />}
        {activeTab === 'API Keys'   && <ApiKeys      apiKey={apiKey} accessToken={session.access_token} onApiKeyChange={activateApiKey} />}
        {activeTab === 'Team & keys' && <CompanyAdministration key={`${session.user.id}:${companyContext.activeCompanyId}`} accessToken={session.access_token} onCompanyContextChange={acceptCompanyCapabilities} />}
        {activeTab === 'Workspace' && <CompanyAdministration personal key={`${session.user.id}:${companyContext.activeCompanyId}`} accessToken={session.access_token} onCompanyContextChange={acceptCompanyCapabilities} />}
        {activeTab === 'Playground' && <Playground   apiKey={apiKey} />}
        {activeTab === 'Docs'       && <Docs />}
        {activeTab === 'Savings'    && <Billing apiKey={apiKey} accessToken={session.access_token} refreshTick={refreshTick} />}
        {activeTab === 'Admin'      && <Admin accessToken={session.access_token} refreshTick={refreshTick} />}
      </main>
      <footer className="pb-8 flex justify-center gap-4 text-[11px] text-brand-muted dark:text-brand-dark-muted">
        <a href="/privacy" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Privacy</a>
        <a href="/terms" className="hover:text-brand-navy dark:hover:text-brand-dark-navy">Terms</a>
      </footer>
    </div>
  )
}
