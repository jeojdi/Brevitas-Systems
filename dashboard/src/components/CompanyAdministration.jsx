import { useCallback, useEffect, useRef, useState } from 'react'
import { redactBrowserError } from '../lib/api.js'
import {
  memberRoleChangeConfirmation,
  memberStatusConfirmation,
  serviceAccountRevocationConfirmation,
} from '../lib/privileged-confirmation.js'

const PAGE_LIMIT = 50
const ROLES = ['company_owner', 'company_admin', 'member', 'billing_admin']
const DEFAULT_SCOPES = ['proxy:invoke', 'usage:write', 'usage:read_own', 'customer:route', 'customer:auto_provision', 'jobs:create', 'jobs:read']

const label = value => String(value || '').replaceAll('_', ' ')

async function companyJson(path, accessToken, { method = 'GET', body, signal } = {}) {
  let response
  try {
    response = await fetch(`/api/admin/company/${path}`, {
      method,
      signal,
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'X-Request-ID': crypto.randomUUID(),
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch (reason) {
    const safeMessage = redactBrowserError(reason instanceof Error ? reason.message : reason)
    throw new Error(safeMessage || 'Company administration request failed')
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const safeDetail = redactBrowserError(
      typeof payload.detail === 'string' ? payload.detail : payload.error)
    throw new Error(safeDetail || `Request failed (${response.status})`)
  }
  return response.json()
}

function PageControls({ page, cursors, onNext, onPrevious }) {
  return <div className="flex items-center justify-between gap-4">
    <p className="annotation">Page {cursors.length + 1} · up to {page.limit || PAGE_LIMIT} rows</p>
    <div className="flex gap-3">
      <button type="button" disabled={!cursors.length} onClick={onPrevious} className="annotation disabled:opacity-40">Previous</button>
      <button type="button" disabled={!page.has_more || !page.next_cursor} onClick={onNext} className="annotation disabled:opacity-40">Next</button>
    </div>
  </div>
}

function OneTimeSecret({ title, value, onClear, description = '' }) {
  const [copyStatus, setCopyStatus] = useState('')
  useEffect(() => { setCopyStatus('') }, [value])
  if (!value) return null

  const copySecret = async () => {
    try {
      if (typeof navigator.clipboard?.writeText !== 'function') throw new Error('unavailable')
      await navigator.clipboard.writeText(value)
      setCopyStatus('Copied')
    } catch {
      setCopyStatus('Copy failed — select the key and copy it manually')
    }
  }

  return <div className="rounded-xl border border-brand-teal/40 bg-brand-teal-dim dark:bg-brand-dark-teal-dim p-4 space-y-3" aria-live="polite">
    <p className="annotation text-brand-teal">{title} · shown once</p>
    {description && <p className="text-xs leading-relaxed text-brand-teal">{description}</p>}
    <code className="block break-all text-xs text-brand-teal ph-no-capture" data-ph-sensitive>{value}</code>
    <div className="flex gap-3">
      <button type="button" onClick={copySecret} className="text-xs text-brand-teal">{copyStatus === 'Copied' ? 'Copied' : 'Copy'}</button>
      <button type="button" onClick={onClear} className="text-xs text-brand-muted">Clear from view</button>
    </div>
    {copyStatus && copyStatus !== 'Copied' && <p role="status" className="text-xs text-brand-muted">{copyStatus}</p>}
  </div>
}

function ConfirmationDialog({ confirmation, busy, error, onCancel, onConfirm }) {
  const dialogRef = useRef(null)
  const cancelButtonRef = useRef(null)

  useEffect(() => {
    if (!confirmation) return undefined
    const previousFocus = document.activeElement
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    cancelButtonRef.current?.focus()
    return () => {
      document.body.style.overflow = previousOverflow
      previousFocus?.focus?.()
    }
  }, [confirmation])

  if (!confirmation) return null

  const handleKeyDown = event => {
    if (event.key === 'Escape' && !busy) {
      event.preventDefault()
      onCancel()
      return
    }
    if (event.key !== 'Tab') return
    const buttons = [...(dialogRef.current?.querySelectorAll('button:not([disabled])') || [])]
    if (!buttons.length) return
    const first = buttons[0]
    const last = buttons[buttons.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  const confirmClass = confirmation.tone === 'danger'
    ? 'bg-red-600 hover:bg-red-700'
    : 'bg-amber-600 hover:bg-amber-700'

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-brand-navy/60 p-4" data-testid="confirmation-backdrop">
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="privileged-confirmation-title"
      aria-describedby="privileged-confirmation-description"
      onKeyDown={handleKeyDown}
      className="w-full max-w-lg rounded-2xl border border-brand-border bg-white p-6 shadow-2xl dark:border-brand-dark-border dark:bg-brand-dark-surface"
    >
      <h3 id="privileged-confirmation-title" className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy">{confirmation.title}</h3>
      <p id="privileged-confirmation-description" className="mt-3 text-sm leading-relaxed text-brand-muted dark:text-brand-dark-muted">{confirmation.description}</p>
      {error && <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-xs text-red-600 dark:bg-red-900/20 dark:text-red-400">{error}</p>}
      <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <button ref={cancelButtonRef} type="button" disabled={busy} onClick={onCancel} className="min-h-11 rounded-xl border border-brand-border px-5 py-2 text-sm disabled:opacity-50 dark:border-brand-dark-border">Cancel</button>
        <button type="button" disabled={busy} onClick={onConfirm} className={`min-h-11 rounded-xl px-5 py-2 text-sm font-medium text-white disabled:opacity-50 ${confirmClass}`}>{busy ? `${confirmation.confirmLabel}…` : confirmation.confirmLabel}</button>
      </div>
    </div>
  </div>
}

function useCursorPage(accessToken, path, enabled) {
  const [page, setPage] = useState({ items: [], next_cursor: '', has_more: false, limit: PAGE_LIMIT })
  const [cursor, setCursor] = useState('')
  const [cursors, setCursors] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (requestedCursor = cursor) => {
    if (!enabled) return
    setLoading(true); setError('')
    try {
      const params = new URLSearchParams({ limit: String(PAGE_LIMIT) })
      if (requestedCursor) params.set('cursor', requestedCursor)
      setPage(await companyJson(`${path}?${params}`, accessToken))
    } catch (reason) {
      setError(reason.message)
    } finally {
      setLoading(false)
    }
  }, [accessToken, cursor, enabled, path])

  useEffect(() => {
    const controller = new AbortController()
    if (enabled) {
      setLoading(true); setError('')
      const params = new URLSearchParams({ limit: String(PAGE_LIMIT) })
      if (cursor) params.set('cursor', cursor)
      companyJson(`${path}?${params}`, accessToken, { signal: controller.signal })
        .then(setPage)
        .catch(reason => { if (reason.name !== 'AbortError') setError(reason.message) })
        .finally(() => setLoading(false))
    }
    return () => controller.abort()
  }, [accessToken, cursor, enabled, path])

  return {
    page, cursors, loading, error, reload: load,
    reset: () => { setCursor(''); setCursors([]) },
    next: () => {
      if (!page.next_cursor) return
      setCursors(stack => [...stack, cursor])
      setCursor(page.next_cursor)
    },
    previous: () => {
      if (!cursors.length) return
      setCursor(cursors[cursors.length - 1])
      setCursors(stack => stack.slice(0, -1))
    },
  }
}

export default function CompanyAdministration({ accessToken, onCompanyContextChange }) {
  const [capabilities, setCapabilities] = useState(null)
  const [capabilityError, setCapabilityError] = useState('')
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('member')
  const [invitationSecret, setInvitationSecret] = useState('')
  const [serviceName, setServiceName] = useState('')
  const [serviceEnvironment, setServiceEnvironment] = useState('production')
  const [serviceSecret, setServiceSecret] = useState('')
  const [mutating, setMutating] = useState(false)
  const [mutationError, setMutationError] = useState('')
  const [confirmation, setConfirmation] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    setCapabilityError('')
    companyJson('capabilities', accessToken, { signal: controller.signal })
      .then(value => {
        setCapabilities(value)
        onCompanyContextChange?.(value)
      })
      .catch(reason => { if (reason.name !== 'AbortError') setCapabilityError(reason.message) })
    return () => controller.abort()
  }, [accessToken, onCompanyContextChange])

  useEffect(() => {
    // Token refresh, auth-user switch, and sign-out/unmount must not leave a
    // one-time invitation or service credential visible in component state.
    setInvitationSecret('')
    setServiceSecret('')
    setInviteEmail('')
    setServiceName('')
    setMutationError('')
    setConfirmation(null)
  }, [accessToken])

  const permissions = new Set(capabilities?.permissions || [])
  const members = useCursorPage(accessToken, 'members', permissions.has('members:read'))
  const invitations = useCursorPage(accessToken, 'invitations', permissions.has('members:invite'))
  const services = useCursorPage(accessToken, 'service-accounts', permissions.has('service_accounts:read'))
  const audit = useCursorPage(accessToken, 'audit-events', permissions.has('audit:read'))

  const mutate = async (operation) => {
    if (mutating) return false
    setMutating(true); setMutationError('')
    try { await operation(); return true } catch (reason) { setMutationError(reason.message); return false }
    finally { setMutating(false) }
  }

  const requestConfirmation = (details, operation) => {
    setMutationError('')
    setConfirmation({ ...details, operation })
  }

  const confirmPrivilegedAction = async () => {
    if (!confirmation?.operation) return
    if (await mutate(confirmation.operation)) setConfirmation(null)
  }

  if (capabilityError) return <p role="alert" className="font-mono text-xs text-red-500">{capabilityError}</p>
  if (!capabilities) return <p className="annotation">// loading company administration…</p>

  return <div className="space-y-12 ph-no-capture" data-ph-sensitive>
    <ConfirmationDialog
      confirmation={confirmation}
      busy={mutating}
      error={mutationError}
      onCancel={() => { if (!mutating) setConfirmation(null) }}
      onConfirm={confirmPrivilegedAction}
    />
    <header>
      <p className="annotation tracking-widest uppercase">Company administration</p>
      <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy mt-2">Invite your team and connect your systems.</h2>
      <p className="text-sm text-brand-muted mt-3">You are signed in as {label(capabilities.role)}. Invite people below; use service accounts for production servers and workers. Administration activity is recorded without names, email addresses, request bodies, or secrets.</p>
    </header>

    {mutationError && <p role="alert" className="font-mono text-xs text-red-500">{mutationError}</p>}

    {permissions.has('members:read') && <section className="space-y-4">
      <div><p className="annotation tracking-widest uppercase">Members</p><h3 className="font-serif text-2xl mt-1">Company access.</h3></div>
      {permissions.has('members:invite') && <div className="rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-5 space-y-3">
        <p className="annotation">Create one-time invitation</p>
        <div className="grid md:grid-cols-[1fr_220px_auto] gap-3">
          <input type="email" value={inviteEmail} onChange={event => setInviteEmail(event.target.value)} maxLength="254" placeholder="person@company.com" className="rounded-xl border border-brand-border dark:border-brand-dark-border px-3 py-2 text-sm" />
          <select value={inviteRole} onChange={event => setInviteRole(event.target.value)} className="rounded-xl border border-brand-border dark:border-brand-dark-border px-3 py-2 text-sm">
            {ROLES.filter(role => role !== 'company_owner').map(role => <option key={role} value={role}>{label(role)}</option>)}
          </select>
          <button type="button" disabled={mutating || !inviteEmail} onClick={() => mutate(async () => {
            const result = await companyJson('invitations', accessToken, { method: 'POST', body: { email: inviteEmail, role: inviteRole, expires_in_hours: 72 } })
            setInvitationSecret(`${window.location.origin}/invite#invite=${result.invitation_token}`); setInviteEmail(''); invitations.reset(); await invitations.reload('')
          })} className="rounded-xl bg-brand-blue text-white px-4 py-2 text-sm disabled:opacity-40">Invite</button>
        </div>
        <dl className="grid gap-2 text-xs text-brand-muted sm:grid-cols-3">
          <div><dt className="font-medium text-brand-navy dark:text-brand-dark-navy">Member</dt><dd>Uses the shared workspace and can view the team roster.</dd></div>
          <div><dt className="font-medium text-brand-navy dark:text-brand-dark-navy">Company admin</dt><dd>Invites and manages people plus service accounts.</dd></div>
          <div><dt className="font-medium text-brand-navy dark:text-brand-dark-navy">Billing admin</dt><dd>Manages billing and can review the administration audit.</dd></div>
        </dl>
        <OneTimeSecret
          title="Invitation link"
          value={invitationSecret}
          description="Copy this private link and send it to the invited address. They will sign in, accept the role, and join this company workspace. Brevitas does not email it automatically yet."
          onClear={() => setInvitationSecret('')}
        />
      </div>}
      {members.error && <p className="font-mono text-xs text-red-500">{members.error}</p>}
      {members.loading ? <p className="annotation">// loading members…</p> : <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
        <table className="w-full min-w-[760px] text-left"><thead><tr>{['Opaque member ID', 'Role', 'Status', 'Actions'].map(value => <th key={value} className="annotation px-4 py-3 border-b border-brand-border dark:border-brand-dark-border">{value}</th>)}</tr></thead>
          <tbody>{members.page.items.map(member => <tr key={member.id} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
            <td className="font-mono text-xs px-4 py-3">{member.id}</td><td className="text-xs px-4 py-3">{label(member.role)}</td><td className="text-xs px-4 py-3">{member.status}</td>
            <td className="px-4 py-3"><div className="flex gap-2">
              {permissions.has('members:manage') && <select aria-label={`Role for ${member.id}`} value={member.role} disabled={mutating} onChange={event => {
                const nextRole = event.target.value
                if (nextRole === member.role) return
                requestConfirmation(memberRoleChangeConfirmation(member, nextRole), async () => {
                  await companyJson(`members/${encodeURIComponent(member.id)}`, accessToken, { method: 'PATCH', body: { role: nextRole, status: member.status } }); members.reset(); await members.reload('')
                })
              }} className="rounded-lg border border-brand-border px-2 py-1 text-xs disabled:opacity-50">
                {ROLES.filter(role => permissions.has('owners:manage') || !['company_owner', 'company_admin'].includes(role)).map(role => <option key={role} value={role}>{label(role)}</option>)}
              </select>}
              {permissions.has('members:manage') && member.status === 'active' && <button type="button" disabled={mutating} onClick={() => requestConfirmation(memberStatusConfirmation(member, 'disabled'), async () => {
                await companyJson(`members/${encodeURIComponent(member.id)}`, accessToken, { method: 'PATCH', body: { role: member.role, status: 'disabled' } }); members.reset(); await members.reload('')
              })} className="text-xs text-amber-600 disabled:opacity-50">Disable</button>}
              {permissions.has('members:manage') && member.status === 'disabled' && <button type="button" disabled={mutating} onClick={() => requestConfirmation(memberStatusConfirmation(member, 'active'), async () => {
                await companyJson(`members/${encodeURIComponent(member.id)}`, accessToken, { method: 'PATCH', body: { role: member.role, status: 'active' } }); members.reset(); await members.reload('')
              })} className="text-xs text-brand-blue disabled:opacity-50">Enable</button>}
              {permissions.has('members:manage') && member.status !== 'removed' && <button type="button" disabled={mutating} onClick={() => requestConfirmation(memberStatusConfirmation(member, 'removed'), async () => {
                await companyJson(`members/${encodeURIComponent(member.id)}`, accessToken, { method: 'PATCH', body: { role: member.role, status: 'removed' } }); members.reset(); await members.reload('')
              })} className="text-xs text-red-500 disabled:opacity-50">Remove</button>}
            </div></td>
          </tr>)}</tbody></table>
      </div>}
      <PageControls page={members.page} cursors={members.cursors} onNext={members.next} onPrevious={members.previous} />
      {permissions.has('members:invite') && <details className="rounded-xl border border-brand-border dark:border-brand-dark-border p-4"><summary className="annotation cursor-pointer">Pending and historical invitations</summary>
        <div className="mt-4 space-y-2">{invitations.page.items.map(item => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 text-xs"><code>{item.id}</code><span>{label(item.role)} · {item.status}</span>{item.status === 'pending' && <button type="button" onClick={() => mutate(async () => { await companyJson(`invitations/${encodeURIComponent(item.id)}/cancel`, accessToken, { method: 'POST' }); invitations.reset(); await invitations.reload('') })} className="text-red-500">Cancel</button>}</div>)}</div>
        <PageControls page={invitations.page} cursors={invitations.cursors} onNext={invitations.next} onPrevious={invitations.previous} />
      </details>}
    </section>}

    {permissions.has('service_accounts:read') && <section className="space-y-4">
      <div><p className="annotation tracking-widest uppercase">Service accounts</p><h3 className="font-serif text-2xl mt-1">Scoped machine identity.</h3></div>
      {permissions.has('service_accounts:manage') && <div className="rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-5 grid md:grid-cols-[1fr_180px_auto] gap-3">
        <input value={serviceName} onChange={event => setServiceName(event.target.value)} maxLength="100" placeholder="Production worker" className="rounded-xl border border-brand-border px-3 py-2 text-sm" />
        <input value={serviceEnvironment} onChange={event => setServiceEnvironment(event.target.value)} maxLength="32" className="rounded-xl border border-brand-border px-3 py-2 text-sm" />
        <button type="button" disabled={mutating || !serviceName} onClick={() => mutate(async () => {
          setServiceSecret('')
          const result = await companyJson('service-accounts', accessToken, { method: 'POST', body: { name: serviceName, environment: serviceEnvironment, scopes: DEFAULT_SCOPES, expires_in_days: 90 } })
          setServiceSecret(result.api_key); setServiceName(''); services.reset(); await services.reload('')
        })} className="rounded-xl bg-brand-blue text-white px-4 py-2 text-sm disabled:opacity-40">Create</button>
      </div>}
      <OneTimeSecret
        title="Service key"
        value={serviceSecret}
        description="Copy this key into the new service now. Brevitas cannot recover it after you clear or leave this page; rotate the account later if it is lost."
        onClear={() => setServiceSecret('')}
      />
      {services.error && <p className="font-mono text-xs text-red-500">{services.error}</p>}
      <div className="grid lg:grid-cols-2 gap-3">{services.page.items.map(account => <article key={account.id} className="rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-5 space-y-3">
        <div><h4 className="font-medium text-brand-navy dark:text-brand-dark-navy">{account.name}</h4><code className="annotation">{account.id}</code></div>
        <p className="text-xs text-brand-muted">{account.environment} · {account.status} · expires {account.expires_at ? new Date(account.expires_at).toLocaleDateString() : 'never'}</p>
        <div className="flex flex-wrap gap-1">{account.scopes.map(scope => <span key={scope} className="rounded-lg bg-brand-blue-dim px-2 py-1 text-[10px] text-brand-blue">{scope}</span>)}</div>
        {permissions.has('service_accounts:manage') && account.status === 'active' && <div className="flex gap-3"><button type="button" onClick={() => mutate(async () => {
          const result = await companyJson(`service-accounts/${encodeURIComponent(account.id)}/rotate-key`, accessToken, { method: 'POST', body: { expires_in_days: 90 } }); setServiceSecret(result.api_key); services.reset(); await services.reload('')
        })} className="text-xs text-brand-blue">Rotate key</button><button type="button" disabled={mutating} onClick={() => requestConfirmation(serviceAccountRevocationConfirmation(account), async () => {
          await companyJson(`service-accounts/${encodeURIComponent(account.id)}`, accessToken, { method: 'DELETE' }); services.reset(); await services.reload('')
        })} className="text-xs text-red-500 disabled:opacity-50">Revoke</button></div>}
      </article>)}</div>
      <PageControls page={services.page} cursors={services.cursors} onNext={services.next} onPrevious={services.previous} />
    </section>}

    {permissions.has('audit:read') && <section className="space-y-4">
      <div><p className="annotation tracking-widest uppercase">Immutable audit evidence</p><h3 className="font-serif text-2xl mt-1">Administration trail.</h3></div>
      {audit.error && <p className="font-mono text-xs text-red-500">{audit.error}</p>}
      <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface"><table className="w-full min-w-[980px] text-left"><thead><tr>{['Time', 'Action', 'Outcome', 'Actor role', 'Target', 'Request ID'].map(value => <th key={value} className="annotation px-4 py-3 border-b border-brand-border dark:border-brand-dark-border">{value}</th>)}</tr></thead>
        <tbody>{audit.page.items.map(event => <tr key={event.id} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border"><td className="text-xs px-4 py-3">{new Date(event.occurred_at).toLocaleString()}</td><td className="font-mono text-xs px-4 py-3">{event.action}</td><td className={`text-xs px-4 py-3 ${event.outcome === 'denied' ? 'text-red-500' : 'text-brand-teal'}`}>{event.outcome}</td><td className="text-xs px-4 py-3">{label(event.actor_role)}</td><td className="font-mono text-xs px-4 py-3">{event.target_type}:{event.target_id}</td><td className="font-mono text-xs px-4 py-3">{event.request_id}</td></tr>)}</tbody></table></div>
      <PageControls page={audit.page} cursors={audit.cursors} onNext={audit.next} onPrevious={audit.previous} />
    </section>}
  </div>
}
