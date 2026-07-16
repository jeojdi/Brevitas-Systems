import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { config } from 'dotenv'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

for (const name of ['.env.local', '.env']) {
  const path = resolve(root, name)
  if (existsSync(path)) config({ path, quiet: true })
}

const env = { ...process.env }
env.VITE_SUPABASE_URL ||= env.NEXT_PUBLIC_SUPABASE_URL
env.VITE_SUPABASE_ANON_KEY ||= env.NEXT_PUBLIC_SUPABASE_ANON_KEY

if (!env.VITE_SUPABASE_URL || !env.VITE_SUPABASE_ANON_KEY) {
  console.error('Dashboard build requires VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY (or the matching NEXT_PUBLIC_ variables).')
  process.exit(1)
}

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const result = spawnSync(npm, ['run', 'build'], {
  cwd: resolve(root, 'dashboard'),
  env,
  stdio: 'inherit',
})

process.exit(result.status ?? 1)
