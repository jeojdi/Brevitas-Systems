import { useState, useEffect, useCallback } from 'react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from 'recharts'

function fmt(n, decimals = 2) {
  return Number(n || 0).toFixed(decimals)
}

function fmtK(n) {
  const v = Number(n || 0)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'k'
  return String(v)
}

function StatCard({ label, value, sub, accent = false }) {
  return (
    <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6">
      <p className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted mb-3">{label}</p>
      <p className={`font-serif text-3xl ${accent ? 'text-brand-teal' : 'text-brand-navy dark:text-brand-dark-navy'} leading-none mb-1`}>{value}</p>
      {sub && <p className="font-mono text-[10px] text-brand-muted dark:text-brand-dark-muted mt-2">{sub}</p>}
    </div>
  )
}

export default function Pipelines({ apiKey }) {
  const [stats, setStats] = useState(null)
  const [selectedPipeline, setSelectedPipeline] = useState(null)
  const [agentStats, setAgentStats] = useState(null)
  const [runStats, setRunStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const loadPipelines = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError('')
    try {
      const r = await fetch('/v1/stats/pipelines', { headers: { 'X-API-Key': apiKey } })
      if (!r.ok) throw new Error(`${r.status}`)
      setStats(await r.json())
      setSelectedPipeline(null)
      setAgentStats(null)
      setRunStats(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  const loadPipelineDetails = useCallback(async (pipeline) => {
    if (!apiKey) return
    setSelectedPipeline(pipeline)
    try {
      const [agents, runs] = await Promise.all([
        fetch(`/v1/stats/agents?pipeline=${encodeURIComponent(pipeline)}`, { headers: { 'X-API-Key': apiKey } }).then(r => r.json()),
        fetch(`/v1/stats/runs?pipeline=${encodeURIComponent(pipeline)}`, { headers: { 'X-API-Key': apiKey } }).then(r => r.json()),
      ])
      setAgentStats(agents)
      setRunStats(runs)
    } catch (e) {
      setError(e.message)
    }
  }, [apiKey])

  useEffect(() => { loadPipelines() }, [loadPipelines])

  if (!apiKey) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">// no API key — create one in the API Keys tab</p>
    </div>
  )

  if (loading) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-[11px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">Loading pipeline data…</p>
    </div>
  )

  if (error) return (
    <div className="pt-12 text-center">
      <p className="font-mono text-xs text-red-500">{error}</p>
    </div>
  )

  const pipelines = stats || []

  // If no pipeline selected, show overview
  if (!selectedPipeline) {
    const totalSaved = pipelines.reduce((sum, p) => sum + (p.tokens_saved || 0), 0)
    const totalCost = pipelines.reduce((sum, p) => sum + (p.cost_saved_usd || 0), 0)
    const totalFee = pipelines.reduce((sum, p) => sum + (p.brevitas_fee_usd || 0), 0)
    const avgQuality = pipelines.length > 0 ? pipelines.reduce((sum, p) => sum + (p.avg_quality || 0), 0) / pipelines.length : 0

    return (
      <div className="space-y-10">
        <div>
          <p className="annotation tracking-widest uppercase mb-4">Pipelines</p>
          <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy leading-tight">
            Savings by pipeline.
          </h2>
          <p className="text-brand-muted dark:text-brand-dark-muted text-base mt-3 max-w-lg leading-relaxed">
            Drill down to see which pipelines and agents deliver the most value from compression.
          </p>
        </div>

        {/* Summary stats */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="Total tokens saved"
            value={fmtK(totalSaved)}
            sub="across all pipelines"
          />
          <StatCard
            label="Total cost saved"
            value={`$${fmt(totalCost, 4)}`}
            sub="provider spend avoided"
            accent
          />
          <StatCard
            label="Active pipelines"
            value={pipelines.length}
            sub="currently tracking"
          />
          <StatCard
            label="Avg quality"
            value={`${fmt(avgQuality, 2)}`}
            sub="across all pipelines"
          />
        </div>

        {/* Pipeline bars */}
        {pipelines.length > 0 && (
          <div>
            <p className="annotation tracking-widest uppercase mb-4">// savings by pipeline</p>
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-6">
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={pipelines}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(100, 100, 100, 0.1)" />
                  <XAxis dataKey="pipeline" />
                  <YAxis />
                  <Tooltip formatter={(value) => fmtK(value)} />
                  <Bar dataKey="tokens_saved" fill="#10b981" name="Tokens saved" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Pipeline list */}
        {pipelines.length > 0 && (
          <div>
            <p className="annotation tracking-widest uppercase mb-4">// pipeline details</p>
            <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-hidden">
              <div className="grid grid-cols-6 gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
                {['Pipeline', 'Calls', 'Tokens saved', 'Quality', 'Cost saved', 'Fee'].map(h => (
                  <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
                ))}
              </div>
              {pipelines.map(p => (
                <button
                  key={p.pipeline}
                  onClick={() => loadPipelineDetails(p.pipeline)}
                  className="w-full grid grid-cols-6 gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors text-left"
                >
                  <span className="font-mono text-xs text-brand-blue">{p.pipeline || '(untagged)'}</span>
                  <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(p.calls)}</span>
                  <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(p.tokens_saved)}</span>
                  <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">{fmt(p.avg_quality, 2)}</span>
                  <span className="font-mono text-xs text-brand-teal">${fmt(p.cost_saved_usd, 4)}</span>
                  <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">${fmt(p.brevitas_fee_usd, 4)}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {pipelines.length === 0 && (
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl p-16 text-center">
            <p className="font-serif text-2xl text-brand-navy-mid dark:text-brand-dark-navy-mid mb-2">No pipeline data yet.</p>
            <p className="annotation">// start tagging calls with pipeline labels to see them here</p>
          </div>
        )}
      </div>
    )
  }

  // Pipeline drilldown view
  const pipelineData = pipelines.find(p => p.pipeline === selectedPipeline)
  const agents = agentStats || []
  const runs = runStats || []

  return (
    <div className="space-y-10">
      <div className="flex items-center justify-between">
        <div>
          <p className="annotation tracking-widest uppercase mb-4">Pipeline details</p>
          <h2 className="font-serif text-4xl text-brand-navy dark:text-brand-dark-navy leading-tight">
            {selectedPipeline}
          </h2>
        </div>
        <button
          onClick={() => setSelectedPipeline(null)}
          className="font-mono text-[11px] tracking-widest uppercase text-brand-muted hover:text-brand-navy dark:hover:text-brand-dark-navy transition-colors"
        >
          Back to pipelines
        </button>
      </div>

      {/* Summary for this pipeline */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Calls"
          value={fmtK(pipelineData?.calls || 0)}
        />
        <StatCard
          label="Tokens saved"
          value={fmtK(pipelineData?.tokens_saved || 0)}
        />
        <StatCard
          label="Cost saved"
          value={`$${fmt(pipelineData?.cost_saved_usd || 0, 4)}`}
          accent
        />
        <StatCard
          label="Fee"
          value={`$${fmt(pipelineData?.brevitas_fee_usd || 0, 4)}`}
        />
      </div>

      {/* Agents in this pipeline */}
      {agents.length > 0 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// agents in this pipeline</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-hidden">
            <div className="grid grid-cols-6 gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Agent', 'Calls', 'Tokens saved', 'Quality', 'Cost saved', 'Fee'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {agents.map(a => (
              <div key={a.agent} className="grid grid-cols-6 gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-blue">{a.agent || '(unnamed)'}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(a.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(a.tokens_saved)}</span>
                <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">{fmt(a.avg_quality, 2)}</span>
                <span className="font-mono text-xs text-brand-teal">${fmt(a.cost_saved_usd, 4)}</span>
                <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">${fmt(a.brevitas_fee_usd, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent runs */}
      {runs.length > 0 && (
        <div>
          <p className="annotation tracking-widest uppercase mb-4">// recent runs</p>
          <div className="bg-white dark:bg-brand-dark-surface border border-brand-border dark:border-brand-dark-border rounded-2xl overflow-hidden">
            <div className="grid grid-cols-5 gap-0 px-5 py-3 border-b border-brand-border dark:border-brand-dark-border">
              {['Run ID', 'Calls', 'Tokens saved', 'Savings %', 'Cost saved'].map(h => (
                <span key={h} className="font-mono text-[10px] tracking-widest uppercase text-brand-muted dark:text-brand-dark-muted">{h}</span>
              ))}
            </div>
            {runs.slice(0, 10).map(r => (
              <div key={r.run_id} className="grid grid-cols-5 gap-0 px-5 py-3.5 border-b border-brand-border dark:border-brand-dark-border last:border-b-0 hover:bg-brand-bg dark:hover:bg-brand-dark-bg transition-colors">
                <span className="font-mono text-xs text-brand-blue truncate" title={r.run_id}>{r.run_id}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(r.calls)}</span>
                <span className="font-mono text-xs text-brand-navy-mid dark:text-brand-dark-navy-mid">{fmtK(r.tokens_saved)}</span>
                <span className="font-mono text-xs text-brand-teal">{fmt(r.avg_savings_pct)}%</span>
                <span className="font-mono text-xs text-brand-muted dark:text-brand-dark-muted">${fmt(r.cost_saved_usd, 4)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
