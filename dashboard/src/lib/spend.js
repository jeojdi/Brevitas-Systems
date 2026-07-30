// The API strips every *_usd key from /v1/stats, /v1/stats/*, /v1/audit and
// /v1/warming for dashboard sessions whose role lacks billing access, and marks
// object payloads with `spend_redacted: true` precisely so a consumer can tell
// "withheld" from "zero". Money the API withheld must render as "Withheld",
// never as a confident $0.00 — NOT QUERIED IS NOT ZERO.
export const WITHHELD = 'Withheld'

// Money the API simply did not state — an absent key, a null, a field this
// deployment's API is too old (or too new) to carry. Distinct from WITHHELD:
// nobody refused it, it just is not a number we can stand behind.
export const UNAVAILABLE = '—'

export function spendRedacted(payload) {
  return payload?.spend_redacted === true
}

// List payloads (/v1/stats/pipelines|agents|runs) cannot carry the flag: a
// stripped row simply lacks its *_usd keys. `undefined` therefore reads as
// withheld, while null and real numbers reach the renderer, which keeps its
// own semantics (e.g. null -> 'Not measured', 0 -> '$0.00').
export function moneyOrWithheld(value, render, redacted = false) {
  if (redacted || value === undefined) return WITHHELD
  return render(value)
}

// CONTRACT A. /v1/stats/pipelines, /v1/stats/agents and /v1/stats/runs used to
// return a bare list, which gave _spend_filtered nowhere to hang the
// spend_redacted flag. They now return {"rows": [...], "spend_redacted": bool}.
// The API (Railway) and this dashboard (Vercel) ship separately and in either
// order, so both shapes have to be correct here: a bare array is read as
// {rows: array, spend_redacted: false}.
export function statsRows(payload) {
  if (Array.isArray(payload)) return { rows: payload, redacted: false }
  if (Array.isArray(payload?.rows)) return { rows: payload.rows, redacted: spendRedacted(payload) }
  // null (not loaded / failed) or an unrecognized shape: no rows, and no claim
  // about redaction either way.
  return { rows: [], redacted: spendRedacted(payload) }
}

// A dollar figure the caller can substantiate, or '—'. `Number(null)` is 0 and
// `Number(undefined)` is NaN, so the naive `Number(v || 0).toFixed(2)` turns
// every absent field into a confident $0.00. A measured 0 still renders.
export function usdOrUnavailable(value, decimals = 4) {
  if (value === null || value === undefined || value === '') return UNAVAILABLE
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `$${parsed.toFixed(decimals)}` : UNAVAILABLE
}

// Sum one *_usd column across rows WITHOUT inventing the missing terms. A
// partial sum rendered as a total is the same lie as a zero rendered as a
// measurement, so any absent/non-finite term collapses the whole thing to null
// and the caller renders 'Withheld' or '—'.
export function sumMoney(rows, key) {
  let total = 0
  for (const row of rows || []) {
    const value = row?.[key]
    if (value === null || value === undefined) return null
    const parsed = Number(value)
    if (!Number.isFinite(parsed)) return null
    total += parsed
  }
  return total
}
