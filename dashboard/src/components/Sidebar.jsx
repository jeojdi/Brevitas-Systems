import MatrixCanvas from './MatrixCanvas.jsx'
import Select from './Select.jsx'

// Left-rail navigation (Nous-Portal-style): a brand-blue vertical sidebar that
// replaces the top tab bar. Grouped sections, an icon + bracketed index per item,
// and a user footer. On < lg it becomes a slide-in drawer controlled by `open`.
// The blue field behind the nav is the same MatrixCanvas glyph animation the
// marketing hero uses (white glyphs on blue).

// Which group each tab lives under, in render order. Tabs absent from the current
// workspace (personal vs enterprise) are filtered out, so one config serves both.
const NAV_GROUPS = [
  { title: 'Dashboard', tabs: ['Overview', 'Projects', 'Repositories', 'Activity', 'Savings'] },
  { title: 'Setup', tabs: ['Connect', 'Workspace', 'Team & keys', 'API Keys', 'Playground'] },
  { title: 'Resources', tabs: ['Docs'] },
  { title: 'Admin', tabs: ['Admin'] },
]

const ICON = {
  Overview: <path d="M3 3v18h18M7 14l3-3 3 3 4-5" />,
  Projects: <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />,
  Repositories: <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />,
  Activity: <path d="M3 12h4l3 8 4-16 3 8h4" />,
  Audit: <path d="M12 3l7 3v6c0 4-3 7-7 9-4-2-7-5-7-9V6l7-3zM9 12l2 2 4-4" />,
  Savings: <><circle cx="12" cy="12" r="9" /><path d="M12 7v10M14.5 9.5C14.5 8.5 13.3 8 12 8s-2.5.5-2.5 1.6S10.7 11.2 12 11.2s2.5.5 2.5 1.6S13.3 15 12 15s-2.5-.5-2.5-1.5" /></>,
  Connect: <><path d="M9 7L5.5 10.5a3.5 3.5 0 000 5L9 19M15 17l3.5-3.5a3.5 3.5 0 000-5L15 5" /><path d="M8.5 15.5l7-7" /></>,
  Workspace: <><circle cx="9" cy="8" r="3" /><path d="M3.5 20c0-3 2.7-5 5.5-5s5.5 2 5.5 5" /><circle cx="17.5" cy="9" r="2" /><path d="M15 15c2.5 0 5.5 1.3 5.5 4" /></>,
  'Team & keys': <><circle cx="9" cy="8" r="3" /><path d="M3.5 20c0-3 2.7-5 5.5-5s5.5 2 5.5 5" /><circle cx="17.5" cy="9" r="2" /></>,
  'API Keys': <><circle cx="8" cy="15" r="4" /><path d="M11 12l8-8 2 2M17 6l2 2" /></>,
  Playground: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></>,
  Docs: <><path d="M6 3h8l5 5v12a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z" /><path d="M14 3v5h5" /></>,
  Admin: <><circle cx="12" cy="12" r="3" /><path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1" /></>,
}

function NavIcon({ tab }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICON[tab] || <circle cx="12" cy="12" r="8" />}
    </svg>
  )
}

const CloseIcon = () => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>

export default function Sidebar({
  tabs, activeTab, onSelect, email,
  companyContext, onSwitchCompany, onSignOut, companySwitching, workspaceKnown,
  open, onClose,
}) {
  const groups = NAV_GROUPS
    .map(group => ({ title: group.title, items: group.tabs.filter(tab => tabs.includes(tab)) }))
    .filter(group => group.items.length)
  const localName = (email || '').split('@')[0]
  const initials = (localName.slice(0, 2) || 'BR').toUpperCase()
  let index = 0

  return (
    <>
      {open && <div className="fixed inset-0 z-40 bg-black/40 lg:hidden" onClick={onClose} aria-hidden="true" />}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-72 flex-col overflow-hidden bg-brand-sidebar text-white transition-transform duration-300 lg:sticky lg:top-0 lg:h-screen lg:self-start lg:z-auto lg:w-64 lg:translate-x-0 ${open ? 'translate-x-0' : '-translate-x-full'}`}
        aria-label="Dashboard navigation"
      >
        <MatrixCanvas className="pointer-events-none absolute inset-0 z-0 h-full w-full opacity-25" />
        <div className="relative z-10 flex items-center justify-between px-5 py-5">
          <a href="/" className="no-underline" aria-label="Brevitas Systems home">
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="h-7 w-auto brightness-0 invert" />
          </a>
          <button className="text-white/70 hover:text-white lg:hidden" onClick={onClose} aria-label="Close menu"><CloseIcon /></button>
        </div>

        <nav className="relative z-10 flex-1 space-y-6 overflow-y-auto px-3 py-2" aria-label="Dashboard sections">
          {groups.map(group => (
            <div key={group.title}>
              <p className="mb-2 ml-1 inline-block border border-white/25 px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest text-white/80">{group.title}</p>
              <ul className="space-y-0.5">
                {group.items.map(tab => {
                  index += 1
                  const active = activeTab === tab
                  return (
                    <li key={tab}>
                      <button
                        type="button"
                        onClick={() => onSelect(tab)}
                        aria-current={active ? 'page' : undefined}
                        className={`flex w-full items-center gap-3 rounded-md px-2 py-2 font-sans text-sm font-medium tracking-wide transition-colors ${active ? 'bg-white/15 text-white' : 'text-white/70 hover:bg-white/10 hover:text-white'}`}
                      >
                        <span className="shrink-0"><NavIcon tab={tab} /></span>
                        <span className="flex-1 truncate text-left">{tab}</span>
                        <span className="tabular-nums text-white/40">[{index}]</span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="relative z-10 space-y-3 border-t border-white/15 bg-brand-sidebar px-3 py-3">
          <div className="flex items-center gap-2">
            {companySwitching && <span className="sr-only" aria-live="polite">Switching workspace…</span>}
            {!workspaceKnown ? (
              <span className="skeleton h-7 flex-1 rounded-md" aria-hidden="true" />
            ) : companyContext.companies.length > 1 ? (
              <Select
                value={companyContext.activeCompanyId}
                onChange={onSwitchCompany}
                disabled={companySwitching}
                ariaLabel="Active workspace"
                wrapperClassName="relative min-w-0 flex-1"
                className="w-full rounded-md border border-white/25 bg-white/10 px-2 py-1.5 text-[11px] text-white"
                options={companyContext.companies.map(company => ({ value: company.company_id, label: company.company_name }))}
              />
            ) : (
              <span className="min-w-0 flex-1 truncate text-[11px] text-white/70">{companyContext.companies?.[0]?.company_name || ''}</span>
            )}
            <button type="button" onClick={onSignOut} className="shrink-0 rounded-md px-2 py-1.5 text-[11px] text-white/70 hover:bg-white/10 hover:text-white">Sign out</button>
          </div>
          <div className="flex items-center gap-3 border-t border-white/15 pt-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center bg-white font-mono text-xs font-bold text-brand-sidebar">{initials}</span>
            <div className="min-w-0">
              <p className="truncate font-mono text-xs uppercase tracking-wide text-white">{localName || 'Account'}</p>
              <p data-ph-sensitive className="truncate text-[10px] text-white/60">{email}</p>
            </div>
          </div>
        </div>
      </aside>
    </>
  )
}
