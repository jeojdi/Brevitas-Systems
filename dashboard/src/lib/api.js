async function responseError(response) {
  const data = await response.json().catch(() => null)
  return new Error(data?.detail || data?.error || `Request failed (${response.status})`)
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
export const fetchKeys = (apiKey, options) => apiJson('/v1/keys', apiKey, options)
export const createKey = (apiKey, name, options = {}) => apiJson('/v1/keys', apiKey, {
  ...options, method: 'POST', body: { name },
})
export const revokeKey = (apiKey, id, options = {}) => apiJson(`/v1/keys/${id}`, apiKey, {
  ...options, method: 'DELETE',
})
export const fetchProvider = (apiKey, options) => apiJson('/v1/provider', apiKey, options)
export const fetchProviders = (apiKey, options) => apiJson('/v1/providers', apiKey, options)
export const fetchOllamaModels = (apiKey, options) => apiJson('/v1/ollama/models', apiKey, options)
export const saveProvider = (apiKey, body, options = {}) => apiJson('/v1/provider', apiKey, {
  ...options, method: 'PUT', body,
})
export const compress = (apiKey, body, options = {}) => apiJson('/v1/compress', apiKey, {
  ...options, method: 'POST', body,
})

export async function streamCompression(apiKey, body, onEvent, { request = fetch, signal } = {}) {
  const response = await request('/v1/compress/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Brevitas-Key': apiKey },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok) throw await responseError(response)
  if (!response.body) throw new Error('Streaming is unavailable in this browser')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const consume = (flush = false) => {
    const lines = buffer.split(/\r?\n/)
    buffer = flush ? '' : lines.pop()
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const event = JSON.parse(line.slice(5).trim())
      if (event.stage === 'error') throw new Error(event.message || 'Compression failed')
      onEvent(event)
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    consume()
  }
  buffer += decoder.decode()
  consume(true)
}

export async function apiKeyId(rawKey) {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(rawKey))
  return [...new Uint8Array(bytes)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}
