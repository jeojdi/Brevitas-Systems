import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_ANON_KEY

export const supabaseMisconfigured = !url || !key

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key)

export async function getOrCreateApiKey(userId) {
  const { data } = await supabase
    .from('user_keys')
    .select('api_key')
    .eq('user_id', userId)
    .single()

  if (data?.api_key) return data.api_key

  const res = await fetch('/v1/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'dashboard' }),
  })
  if (!res.ok) throw new Error('Failed to create API key')
  const { api_key } = await res.json()

  await supabase.from('user_keys').insert({ user_id: userId, api_key })

  return api_key
}
