import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { fetchBreakdown, fetchRepoMeta, upsertRepoMeta, deleteRepoMeta } from '../lib/api.js'
import { UNAVAILABLE, WITHHELD, spendRedacted, usdOrUnavailable } from '../lib/spend.js'

const number = n => Number(n || 0).toLocaleString()
// A priced row states a number; a row the pricer could not price states null,
// which is 'Unpriced'. Anything else (a stripped key, a non-numeric value) is
// not a dollar figure we can stand behind, so it renders '—' rather than $0.
const usd = n => n === null ? 'Unpriced' : usdOrUnavailable(n)

// Repositories are discovered from usage (fetchBreakdown); repo_meta only decorates
// them with a friendly display name (and lets a user pre-add a repo before any usage
// arrives). The canonical `repo` key stays the identity — renaming never moves usage.
export default function Projects({ apiKey, refreshTick }) {
  const [rows, setRows] = useState([])
  const [meta, setMeta] = useState([])
  // /v1/stats/breakdown strips *_usd keys and sets spend_redacted for roles
  // without billing access; withheld money must not render as $0.0000.
  const [withheld, setWithheld] = useState(false)
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // Add / rename UI.
  const [showAdd, setShowAdd] = useState(false)
  const [addRepo, setAddRepo] = useState('')
  const [addName, setAddName] = useState('')
  const [editing, setEditing] = useState('')   // canonical repo key being renamed
  const [editName, setEditName] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const controllerRef = useRef(null)

  const load = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    setError('')
    try {
      const [data, metaData] = await Promise.all([
        fetchBreakdown(apiKey, { signal: controller.signal }),
        // Aliases are best-effort: if the repo_meta table is not migrated yet, the
        // page still shows discovered repos, just without display names.
        fetchRepoMeta(apiKey, { signal: controller.signal }).catch(() => ({ repos: [] })),
      ])
      if (controllerRef.current === controller) {
        setRows(data.rows || [])
        setWithheld(spendRedacted(data))
        setMeta(Array.isArray(metaData?.repos) ? metaData.repos : [])
      }
    } catch (error) {
      if (controllerRef.current === controller && error.name !== 'AbortError') setError(error.message)
    } finally {
      if (controllerRef.current === controller) setLoading(false)
    }
  }, [apiKey])

  useEffect(() => {
    load()
    return () => controllerRef.current?.abort()
  }, [load, refreshTick])

  const metaMap = useMemo(() => new Map(meta.map(item => [item.repo, item])), [meta])

  // Roll-ups add only terms the API actually stated. A row missing actual_cost_usd or
  // native_cache_discount_usd (stripped for withheld roles, or absent on an older API)
  // would contribute a silent 0; `spendKnown`/`cacheKnown` go false on the first such
  // row so the card renders '—'. Savings are the measured native-cache discount
  // (verified_savings_usd is dark in practice — see the Overview fix).
  const projects = useMemo(() => Object.values(rows.reduce((all, row) => {
    const name = row.repo || row.project || 'Unattributed'
    const project = all[name] ||= {
      name, calls: 0, spend: 0, cacheDiscount: 0,
      unpriced: 0, spendKnown: true, cacheKnown: true, rows: [],
    }
    project.calls += Number(row.calls || 0)
    const spend = row.actual_cost_usd === null ? 0 : Number(row.actual_cost_usd)
    if (Number.isFinite(spend)) project.spend += spend
    else project.spendKnown = false
    const cache = row.native_cache_discount_usd === null ? 0 : Number(row.native_cache_discount_usd)
    if (Number.isFinite(cache)) project.cacheDiscount += cache
    else project.cacheKnown = false
    project.unpriced += Number(row.unpriced_calls || 0)
    project.rows.push(row)
    return all
  }, {})), [rows])

  // Merge aliases + inject pre-added repos that have no usage yet, then attach the
  // display label (display_name falls back to the canonical repo key).
  const displayProjects = useMemo(() => {
    const byName = new Map(projects.map(project => [project.name, project]))
    for (const item of meta) {
      if (item.added && !byName.has(item.repo)) {
        byName.set(item.repo, {
          name: item.repo, calls: 0, spend: 0, cacheDiscount: 0,
          unpriced: 0, spendKnown: true, cacheKnown: true, rows: [], addedOnly: true,
        })
      }
    }
    return Array.from(byName.values()).map(project => ({
      ...project, label: metaMap.get(project.name)?.display_name || project.name,
    }))
  }, [projects, meta, metaMap])

  const saveMeta = async ({ repo, display_name, added }) => {
    if (!repo) return
    setBusy(true); setActionError('')
    try {
      await upsertRepoMeta(apiKey, { repo, display_name, added })
      setShowAdd(false); setAddRepo(''); setAddName(''); setEditing(''); setEditName('')
      await load()
    } catch (reason) { setActionError(reason.message) } finally { setBusy(false) }
  }
  const removeMeta = async repo => {
    setBusy(true); setActionError('')
    try { await deleteRepoMeta(apiKey, repo); if (selected === repo) setSelected(''); await load() }
    catch (reason) { setActionError(reason.message) } finally { setBusy(false) }
  }
  const beginEdit = project => { setEditing(project.name); setEditName(metaMap.get(project.name)?.display_name || '') }

  const savedPct = project => {
    const gross = project.spend + project.cacheDiscount
    return gross > 0 ? (100 * project.cacheDiscount / gross).toFixed(1) : null
  }

  const inputClass = 'rounded-lg border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface px-3 py-2 text-xs text-brand-navy dark:text-brand-dark-navy'
  const btnClass = 'rounded-lg bg-brand-blue px-3 py-2 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40 transition-opacity'

  if (loading) return <p className="annotation pt-8">// loading repositories…</p>
  if (error && !rows.length) return <div className="pt-8"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation mt-3 hover:text-brand-blue">retry</button></div>

  const current = displayProjects.find(project => project.name === selected)
  if (current) return (
    <div className="space-y-8">
      {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
      <button onClick={() => setSelected('')} className="annotation hover:text-brand-blue">← all repositories</button>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-sans text-4xl font-semibold text-brand-navy dark:text-brand-dark-navy">{current.label}</h2>
          {current.label !== current.name && <p className="annotation mt-1">{current.name}</p>}
        </div>
        {editing === current.name ? (
          <div className="flex flex-wrap items-center gap-2">
            <input value={editName} onChange={event => setEditName(event.target.value)} placeholder="Display name" className={inputClass} />
            <button disabled={busy} onClick={() => saveMeta({ repo: current.name, display_name: editName, added: false })} className={btnClass}>Save</button>
            <button onClick={() => { setEditing(''); setEditName('') }} className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy">Cancel</button>
          </div>
        ) : (
          <button onClick={() => beginEdit(current)} className="annotation hover:text-brand-blue">Edit name</button>
        )}
      </div>
      {actionError && <p className="font-mono text-xs text-red-500">{actionError}</p>}
      {current.rows.length === 0 ? (
        <p className="annotation">// no usage recorded for this repository yet</p>
      ) : (
      <div className="overflow-x-auto rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface">
        <table className="w-full min-w-[760px] text-left">
          <thead><tr className="border-b border-brand-border dark:border-brand-dark-border">{['Client', 'Provider / model', 'Operation', 'Calls', 'Cache savings', 'Provider spend'].map(label => <th key={label} className="annotation px-4 py-3">{label}</th>)}</tr></thead>
          <tbody>{current.rows.map((row, index) => <tr key={`${row.client}-${row.provider}-${row.model}-${index}`} className="border-b last:border-0 border-brand-border dark:border-brand-dark-border">
            <td className="font-mono text-xs px-4 py-3 text-brand-navy dark:text-brand-dark-navy">{row.client || row.source || 'Unattributed'}{row.environment ? ` / ${row.environment}` : ''}{row.agent ? ` / ${row.agent}` : ''}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-blue">{row.gateway ? `${row.gateway} → ` : ''}{row.provider || 'unknown'} / {row.model || 'unknown'}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-muted">{row.operation}</td>
            <td className="font-mono text-xs px-4 py-3">{number(row.calls)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-teal">{withheld ? WITHHELD : usd(row.native_cache_discount_usd)}</td>
            <td className="font-mono text-xs px-4 py-3 text-brand-navy-mid dark:text-brand-dark-navy-mid">{withheld ? WITHHELD : row.unpriced_calls === row.calls ? 'Unpriced' : usd(row.actual_cost_usd)}</td>
          </tr>)}</tbody>
        </table>
      </div>
      )}
    </div>
  )

  return <div className="space-y-8">
    {error && <div className="flex flex-wrap items-center gap-3 rounded-xl border border-red-200 dark:border-red-900/40 p-4"><p className="font-mono text-xs text-red-500">{error}</p><button onClick={load} className="annotation hover:text-brand-blue">retry</button></div>}
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><h2 className="font-sans text-4xl font-semibold text-brand-navy dark:text-brand-dark-navy">Every codebase and agent.</h2><p className="text-brand-muted mt-3">Runtime usage discovered through AgentMap integrations.</p></div>
      <button onClick={() => { setShowAdd(value => !value); setActionError('') }} className={btnClass}>{showAdd ? 'Cancel' : '+ Add repository'}</button>
    </div>

    <AnimatePresence initial={false}>
    {showAdd && (
      <motion.div
        initial={{ opacity: 0, height: 0 }}
        animate={{ opacity: 1, height: 'auto' }}
        exit={{ opacity: 0, height: 0 }}
        transition={{ type: 'spring', bounce: 0, duration: 0.3 }}
        className="overflow-hidden"
      >
        <div className="rounded-2xl border border-brand-border dark:border-brand-dark-border bg-white dark:bg-brand-dark-surface p-5 space-y-3">
          <p className="annotation">Add a repository. Use the exact repo key your integration reports (BREVITAS_REPO); usage attaches automatically once calls arrive.</p>
          <div className="flex flex-wrap items-center gap-2">
            <input value={addRepo} onChange={event => setAddRepo(event.target.value)} placeholder="repo key (e.g. acme/api)" className={`${inputClass} min-w-56`} />
            <input value={addName} onChange={event => setAddName(event.target.value)} placeholder="Display name (optional)" className={`${inputClass} min-w-56`} />
            <button disabled={busy || !addRepo.trim()} onClick={() => saveMeta({ repo: addRepo.trim(), display_name: addName.trim(), added: true })} className={btnClass}>Add</button>
          </div>
        </div>
      </motion.div>
    )}
    </AnimatePresence>
    {actionError && <p className="font-mono text-xs text-red-500">{actionError}</p>}

    {displayProjects.length === 0 ? (
      <div className="pt-8 text-center"><p className="font-sans text-2xl font-semibold text-brand-navy dark:text-brand-dark-navy">No repositories yet.</p><p className="annotation mt-2">// set BREVITAS_REPO and make an AI call, or add one above</p></div>
    ) : (
    <div className="grid md:grid-cols-2 gap-4">{displayProjects.map(project => <div key={project.name} className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6 transition-colors hover:border-brand-blue">
      <div className="flex items-start justify-between gap-3">
        <button onClick={() => setSelected(project.name)} className="min-w-0 flex-1 text-left">
          <p className="font-sans text-2xl font-semibold text-brand-navy dark:text-brand-dark-navy truncate">{project.label}</p>
          {project.label !== project.name && <p className="annotation mt-0.5 truncate">{project.name}</p>}
        </button>
        <div className="flex shrink-0 items-center gap-3">
          <button onClick={() => beginEdit(project)} className="annotation hover:text-brand-blue">Edit</button>
          {project.addedOnly && <button disabled={busy} onClick={() => removeMeta(project.name)} className="annotation hover:text-red-500">Remove</button>}
        </div>
      </div>
      {editing === project.name ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input value={editName} onChange={event => setEditName(event.target.value)} placeholder="Display name" className={`${inputClass} min-w-48`} />
          <button disabled={busy} onClick={() => saveMeta({ repo: project.name, display_name: editName, added: false })} className={btnClass}>Save</button>
          <button onClick={() => { setEditing(''); setEditName('') }} className="annotation hover:text-brand-navy dark:hover:text-brand-dark-navy">Cancel</button>
        </div>
      ) : project.addedOnly ? (
        <p className="annotation mt-2">// no usage yet — calls appear here once this repo sends traffic</p>
      ) : (
        <>
          <p className="annotation mt-2">{number(project.calls)} calls{withheld || !project.cacheKnown || !project.spendKnown || savedPct(project) === null ? '' : ` · ${savedPct(project)}% saved through caching`}</p>
          <div className="flex flex-wrap gap-6 mt-5"><div><p className="annotation">Provider spend</p><p className="font-mono text-brand-navy-mid dark:text-brand-dark-navy-mid">{withheld ? WITHHELD : project.unpriced === project.calls ? 'Unpriced' : !project.spendKnown ? UNAVAILABLE : usd(project.spend)}</p></div><div><p className="annotation">Cache savings</p><p className="font-mono text-brand-teal">{withheld ? WITHHELD : !project.cacheKnown ? UNAVAILABLE : usd(project.cacheDiscount)}</p></div></div>
        </>
      )}
    </div>)}</div>
    )}
  </div>
}
