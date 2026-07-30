// Self-session detection for the key list. The hosted RPC
// (company_admin_dashboard_keys_page, 202607170009) returns `key_prefix as
// prefix` — the raw key's first 12 characters — and NO fingerprint field; the
// SQLite listing additionally carries `fingerprint` (sha256(key)[:16], the same
// digest apiKeyId() computes in the browser). Rows must never match on an
// empty prefix/fingerprint: ''.startsWith('') is true, which is exactly the
// bug that left every Revoke button disabled and labelled 'Active'.
export function isActiveKeyRow(row, { apiKey = '', apiKeyHash = '' } = {}) {
  if (!row || typeof row !== 'object') return false
  if (row.prefix && typeof apiKey === 'string' && apiKey.startsWith(row.prefix)) {
    return true
  }
  if (row.fingerprint && typeof apiKeyHash === 'string'
      && apiKeyHash.startsWith(row.fingerprint)) {
    return true
  }
  return false
}
