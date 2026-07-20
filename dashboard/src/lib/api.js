export function redactBrowserError(value) {
  const message = String(value || '').slice(0, 500)
  return message
    .replace(/\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}/gi, '[REDACTED]')
    .replace(/\b(?:sk|rk|pk|bvt|whsec|xox[baprs]|gh[opusr]|sb_secret)[_-][A-Za-z0-9_-]{6,}/gi, '[REDACTED]')
    .replace(/(^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?=$|[^A-Za-z0-9_-])/g, '$1[REDACTED]')
    .replace(/\b(?:api[_-]?key|authorization|pass(?:word)|secret|token)\s*[:=]\s*[^\s,;]+/gi, '[REDACTED]')
    .replace(/[\r\n\x00-\x1f\x7f]/g, '')
}

async function responseError(response) {
  const data = await response.json().catch(() => null)
  const detail = redactBrowserError(data?.detail || data?.error)
  return new Error(detail || `Request failed (${response.status})`)
}

export async function apiJson(path, apiKey, { body, request = fetch, headers, ...options } = {}) {
  const response = await request(path, {
    ...options,
    headers: {
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      'X-Brevitas-Key': apiKey,
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) throw await responseError(response)
  return response.json()
}

export const fetchStats = (apiKey, options) => apiJson('/v1/stats', apiKey, options)
export const fetchBreakdown = (apiKey, options) => apiJson('/v1/stats/breakdown', apiKey, options)
const managementJson = async (path, accessToken, { body, request = fetch, headers, ...options } = {}) => {
  const response = await request(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${accessToken}`,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) throw await responseError(response)
  return response.json()
}

export const fetchKeys = (accessToken, options) => managementJson('/v1/keys', accessToken, options)
export const createKey = (accessToken, name, options = {}) => managementJson('/v1/keys', accessToken, {
  ...options, method: 'POST', body: { name },
})
export const revokeKey = (accessToken, id, options = {}) => managementJson(`/v1/keys/${id}`, accessToken, {
  ...options, method: 'DELETE',
})
export const fetchProvider = (apiKey, options) => apiJson('/v1/provider', apiKey, options)
export const fetchProviders = (apiKey, options) => apiJson('/v1/providers', apiKey, options)
export const fetchOllamaModels = (apiKey, options) => apiJson('/v1/ollama/models', apiKey, options)
export const saveProvider = (apiKey, body, options = {}) => apiJson('/v1/provider', apiKey, {
  ...options, method: 'PUT', body,
})

async function billingJson(path, accessToken, { request = fetch, ...options } = {}) {
  const response = await request(path, {
    ...options,
    headers: { Authorization: `Bearer ${accessToken}`, ...options.headers },
  })
  if (!response.ok) {
    const error = await responseError(response)
    error.status = response.status
    throw error
  }
  return response.json()
}

export const fetchBillingStatus = (accessToken, options) =>
  billingJson('/api/billing/status', accessToken, options)
export const startBillingCheckout = (accessToken, options = {}) =>
  billingJson('/api/billing/checkout', accessToken, { ...options, method: 'POST' })
export const openBillingPortal = (accessToken, options = {}) =>
  billingJson('/api/billing/portal', accessToken, { ...options, method: 'POST' })
export const compress = (apiKey, body, options = {}) => apiJson('/v1/compress', apiKey, {
  ...options, method: 'POST', body,
})

export const streamCompression = (apiKey, body, onEvent, options) =>
  streamEvents('/v1/compress/stream', apiKey, body, onEvent, options)

// Interactive Playground chat — same SSE shape as compression, different endpoint.
export const streamPlaygroundChat = (apiKey, body, onEvent, options) =>
  streamEvents('/v1/playground/stream', apiKey, body, onEvent, options)

async function streamEvents(path, apiKey, body, onEvent, { request = fetch, signal } = {}) {
  let response
  try {
    response = await request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Brevitas-Key': apiKey },
      body: JSON.stringify(body),
      signal,
    })
  } catch (error) {
    throw new Error(redactBrowserError(error?.message) || 'Streaming request failed')
  }
  if (!response.ok) throw await responseError(response)
  if (!response.body) throw new Error('Streaming is unavailable in this browser')

  let reader
  try {
    reader = response.body.getReader()
  } catch {
    throw new Error('Streaming is unavailable in this browser')
  }
  const decoder = new TextDecoder()
  let buffer = ''
  const consume = (flush = false) => {
    const lines = buffer.split(/\r?\n/)
    buffer = flush ? '' : lines.pop()
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      let event
      try {
        event = JSON.parse(line.slice(5).trim())
      } catch {
        throw new Error('Invalid streaming response')
      }
      if (event.stage === 'error') {
        throw new Error(redactBrowserError(event.message) || 'Compression failed')
      }
      try {
        onEvent(event)
      } catch (error) {
        throw new Error(redactBrowserError(error?.message) || 'Streaming event handler failed')
      }
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      consume()
    }
  } catch (error) {
    const message = redactBrowserError(error?.message)
    throw new Error(message || 'Streaming response failed')
  }
  buffer += decoder.decode()
  consume(true)
}

export async function apiKeyId(rawKey) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(rawKey))
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}
