import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { fetchAudit } from './api.js'

const source = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})

test('audit fetcher hits /v1/audit with the api-key header contract', async () => {
  const calls = []
  const report = { schema: 'brevitas.audit.v1', score: 0, verdict: 'well_optimized', capabilities: [], caveats: [] }
  const result = await fetchAudit('bvt_active', {
    request: async (path, options) => {
      calls.push([path, options.headers['X-Brevitas-Key']])
      return json(report)
    },
  })
  assert.deepEqual(calls, [['/v1/audit', 'bvt_active']])
  assert.deepEqual(result, report)
})

test('audit page is registered in both workspace tab lists', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /PERSONAL_TABS = \[[^\]]*'Audit'/)
  assert.match(app, /ENTERPRISE_TABS = \[[^\]]*'Audit'/)
  assert.match(app, /activeTab === 'Audit'[\s\S]{0,40}<Audit apiKey=\{apiKey\} refreshTick=\{refreshTick\}/)
})

test('audit UI celebrates the well-optimized case and renders every verdict honestly', async () => {
  const audit = await source('Audit')
  assert.match(audit, /already well-optimized/)
  assert.match(audit, /verdict === 'well_optimized'/)
  for (const verdict of ['already_implemented', 'partial', 'opportunity', 'not_applicable', 'unknown']) {
    assert.match(audit, new RegExp(`\\b${verdict}\\b`))
  }
  assert.match(audit, /Not measured/)
  assert.match(audit, /caveats\.map/)
  assert.match(audit, /evidence/)
})

test('audit UI never claims savings for a report that measured nothing', async () => {
  const audit = await source('Audit')
  // The server emits top-level verdict "not_measured" whenever zero capabilities
  // were measurable — guaranteed for every new key before traffic flows. That
  // report must NOT render an opportunity headline.
  assert.match(audit, /not_measured: \{/)
  assert.match(audit, /Nothing measured yet\./)
  // Unrecognized top-level verdicts fall back to the no-claim state, never to
  // a savings claim.
  assert.match(audit, /REPORT_VERDICTS\[report\?\.verdict\] \|\| REPORT_VERDICTS\.not_measured/)
  // "Some savings" is only reachable via an explicit moderate_opportunity verdict.
  assert.match(audit, /moderate_opportunity: \{\n\s*headline: 'Some savings are still on the table\.'/)
  // The score tile shows a dash, not a "0" that would read as fully optimized.
  assert.match(audit, /verdict !== 'not_measured'/)
  assert.match(audit, /not measured yet/)
})

test('audit UI degrades gracefully and never fabricates dollar figures or charts', async () => {
  const audit = await source('Audit')
  // Deployments without the server-side audit route show a zero-state, not an error.
  assert.match(audit, /\[404, 501\]\.includes\(e\.status\)/)
  assert.match(audit, /No audit available yet\./)
  // Dollar estimates require traffic data; the audit page prints none at all.
  assert.doesNotMatch(audit, /\$\d|\btoFixed\b|_usd\b/)
  // Badges and a table suffice — no chart series to collide with ui.check.mjs.
  assert.doesNotMatch(audit, /recharts|type="monotone"|BarChart|LineChart|AreaChart/)
})
