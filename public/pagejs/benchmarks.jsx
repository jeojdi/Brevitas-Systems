
const INK = 'var(--bronze)';
const RIVAL_A = 'var(--stone)';
const RIVAL_B = 'var(--stone-2)';
const GRID = 'var(--line)';
const TEXT = 'var(--stone)';
const TEXT_STRONG = 'var(--fg)';
const mono = "'JetBrains Mono', ui-monospace, monospace";

const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

// Headline speed-ups (Brevitas Splice technical brief, Sept 2026).
const HEADLINES = [
  { label: 'Agent compaction', val: '12.4', unit: '×', sub: '25% of a 32k session deleted · Qwen2.5-14B' },
  { label: 'Steady per-edit, text', val: '1.6', unit: '×', sub: 'flat from 8k to 128k tokens · Qwen2.5-7B' },
  { label: 'Media re-ingest avoided', val: '220', unit: '×', sub: 'peak · delete ≤20% of a video vs re-ingest' },
  { label: 'Correctness gate', val: '<0.05', unit: ' KL', sub: 'pooled p95 vs a fresh re-ingest' },
];

// Per-edit and all-in speedup vs context length. 1x H100, Qwen2.5-7B-Instruct-1M, 10 edits, repair budget 0.15.
const CTX = [
  { ctx: '8k', per: 1.50, all: 0.85 },
  { ctx: '16k', per: 1.61, all: 0.76 },
  { ctx: '32k', per: 1.63, all: 0.66 },
  { ctx: '64k', per: 1.61, all: 0.59 },
  { ctx: '128k', per: 1.51, all: 0.54 },
];

function ContextChart() {
  const W = 680, H = 280, L = 44, R = 16, T = 16, B = 36;
  const max = 2.0;
  const x = i => L + (i + 0.5) * ((W - L - R) / CTX.length);
  const y = v => T + (1 - v / max) * (H - T - B);
  const path = key => CTX.map((d, i) => `${i ? 'L' : 'M'}${x(i)},${y(d[key])}`).join(' ');
  return (
    <figure className="splice-fig fade-up">
      <h3>Speed-up per edit stays flat as context grows</h3>
      <p className="sub">1× H100 · Qwen2.5-7B-Instruct-1M · 10 edits, each removing 5% of the context · vs re-prefilling from the edit point</p>
      <div className="splice-legend">
        <span><i style={{ background: INK }}></i>Per edit (one-time setup excluded)</span>
        <span><i style={{ background: RIVAL_A }}></i>All-in over 10 edits (setup included)</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Line chart: per-edit speed-up is 1.50x to 1.63x from 8k to 128k tokens; all-in speed-up over 10 edits falls from 0.85x to 0.54x.">
        {[0, 0.5, 1, 1.5, 2].map(v => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} style={{ stroke: GRID }} strokeWidth={v === 1 ? 1.5 : 1} strokeDasharray={v === 1 ? '4 4' : ''} />
            <text x={L - 8} y={y(v) + 4} textAnchor="end" style={{ fill: TEXT, fontFamily: mono, fontSize: 11 }}>{v.toFixed(1)}×</text>
          </g>
        ))}
        <text x={W - R} y={y(1) - 6} textAnchor="end" style={{ fill: TEXT, fontSize: 11 }}>break-even</text>
        <path d={path('per')} fill="none" style={{ stroke: INK }} strokeWidth="2.5" />
        <path d={path('all')} fill="none" style={{ stroke: RIVAL_A }} strokeWidth="2.5" />
        {CTX.map((d, i) => (
          <g key={d.ctx}>
            <circle cx={x(i)} cy={y(d.per)} r="4" style={{ fill: INK }} />
            <circle cx={x(i)} cy={y(d.all)} r="4" style={{ fill: RIVAL_A }} />
            <text x={x(i)} y={y(d.per) - 10} textAnchor="middle" style={{ fill: TEXT_STRONG, fontFamily: mono, fontSize: 11 }}>{d.per.toFixed(2)}×</text>
            <text x={x(i)} y={y(d.all) + 18} textAnchor="middle" style={{ fill: TEXT, fontFamily: mono, fontSize: 11 }}>{d.all.toFixed(2)}×</text>
            <text x={x(i)} y={H - 12} textAnchor="middle" style={{ fill: TEXT, fontFamily: mono, fontSize: 11 }}>{d.ctx}</text>
          </g>
        ))}
      </svg>
      <figcaption>Output quality on every point: pooled p95 KL between 0.0001 and 0.0005 against a fresh re-ingest, two orders of magnitude under the 0.05 threshold.</figcaption>
    </figure>
  );
}

// Head to head on 12 real OpenHands agent sessions per cell, Qwen2.5-14B, 1x H100.
const H2H = [
  { cell: '25% of 16k', splice: 9.58, info: 5.07, blend: 4.64 },
  { cell: '25% of 32k', splice: 12.43, info: 5.57, blend: 4.47 },
  { cell: '33% of 32k', splice: 7.49, info: 3.15, blend: 1.58 },
  { cell: '73% of 16k', splice: 1.41, info: 0.83, blend: 0.69 },
];

function HeadToHeadChart() {
  const W = 680, rowH = 58, L = 110, R = 56, T = 8;
  const H = T + H2H.length * rowH + 24;
  const max = 13;
  const w = v => (v / max) * (W - L - R);
  const bars = [['splice', INK, 'Splice'], ['info', RIVAL_B, 'InfoFlow'], ['blend', RIVAL_A, 'CacheBlend']];
  return (
    <figure className="splice-fig fade-up">
      <h3>Same sessions, same quality check</h3>
      <p className="sub">1× H100 · Qwen2.5-14B · 12 real OpenHands agent sessions per row · each method at the smallest recompute fraction that passes · higher is faster</p>
      <div className="splice-legend">
        {bars.map(([k, c, label]) => <span key={k}><i style={{ background: c }}></i>{label}</span>)}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Grouped bar chart of speed-up over re-ingest. 25% of 16k: Splice 9.6x, InfoFlow 5.1x, CacheBlend 4.6x. 25% of 32k: 12.4x, 5.6x, 4.5x. 33% of 32k: 7.5x, 3.2x, 1.6x. 73% of 16k: 1.4x, 0.8x, 0.7x.">
        <line x1={L + w(1)} x2={L + w(1)} y1={T} y2={H - 24} style={{ stroke: GRID }} strokeDasharray="4 4" />
        <text x={L + w(1)} y={H - 8} textAnchor="middle" style={{ fill: TEXT, fontFamily: mono, fontSize: 11 }}>1×</text>
        {H2H.map((d, r) => (
          <g key={d.cell}>
            <text x={L - 12} y={T + r * rowH + 28} textAnchor="end" style={{ fill: TEXT_STRONG, fontSize: 12.5 }}>{d.cell}</text>
            {bars.map(([k, c], j) => (
              <g key={k}>
                <rect x={L} y={T + r * rowH + 6 + j * 15} width={Math.max(w(d[k]), 2)} height="12" rx="2" style={{ fill: c }} />
                <text x={L + w(d[k]) + 6} y={T + r * rowH + 16 + j * 15} style={{ fill: j === 0 ? TEXT_STRONG : TEXT, fontFamily: mono, fontSize: 11 }}>{d[k].toFixed(1)}×</text>
              </g>
            ))}
          </g>
        ))}
      </svg>
      <figcaption>Deleted share of the context on the left. Speed-up is over a full re-ingest of the edited sequence on the same GPU. InfoFlow and CacheBlend were reimplemented inside our harness rather than run from their released code. A seam-repair baseline (programmable-kv) is faster than all three at 25% on one session sample, at 14.8× and 31.1×, but did not reproduce on a second sample and did not pass at 50%.</figcaption>
    </figure>
  );
}

// Pooled p95 teacher-forced KL for passing cells vs the 0.05 threshold.
const QUAL = [
  { label: '7B-1M, 32k', kl: 0.00009 },
  { label: '7B-1M, 8k', kl: 0.00053 },
  { label: '14B, 8k', kl: 0.0018 },
  { label: '72B, 8k', kl: 0.0084 },
  { label: 'Audio delete 16%', kl: 0.025 },
  { label: 'Video swap, 97k', kl: 0.034 },
  { label: 'Video delete 20%', kl: 0.038 },
  { label: '14B, 25% of 16k', kl: 0.0404 },
  { label: 'SGLang, 25% of 16k', kl: 0.0485 },
];

function QualityChart() {
  const W = 680, rowH = 26, L = 150, R = 24, T = 8;
  const H = T + QUAL.length * rowH + 30;
  const max = 0.06;
  const x = v => L + (v / max) * (W - L - R);
  return (
    <figure className="splice-fig fade-up">
      <h3>Every result on this page passed the quality check</h3>
      <p className="sub">Pooled p95 teacher-forced KL divergence between the edited cache and a fresh re-ingest · lower is closer · pass line at 0.05</p>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Dot plot of KL divergence per benchmark, all below the 0.05 pass line, ranging from 0.00009 to 0.0485.">
        {[0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06].map(v => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={T} y2={H - 26} style={{ stroke: GRID }} strokeWidth={v === 0.05 ? 2 : 1} strokeDasharray={v === 0.05 ? '5 4' : ''} />
            <text x={x(v)} y={H - 10} textAnchor="middle" style={{ fill: v === 0.05 ? TEXT_STRONG : TEXT, fontFamily: mono, fontSize: 11 }}>{v === 0.05 ? '0.05 pass' : v.toFixed(2)}</text>
          </g>
        ))}
        {QUAL.map((d, i) => (
          <g key={d.label}>
            <text x={L - 12} y={T + i * rowH + 17} textAnchor="end" style={{ fill: TEXT_STRONG, fontSize: 12.5 }}>{d.label}</text>
            <line x1={x(0)} x2={x(d.kl)} y1={T + i * rowH + 13} y2={T + i * rowH + 13} style={{ stroke: INK }} strokeOpacity="0.35" strokeWidth="2" />
            <circle cx={x(d.kl)} cy={T + i * rowH + 13} r="5" style={{ fill: INK }} />
          </g>
        ))}
      </svg>
      <figcaption>A control arm that recomputes every row reads 0.00001–0.00004 on text and exactly 0.0 inside SGLang, so the check can tell a correct cache from a damaged one. Media controls sit at a kernel noise floor of about 0.002–0.007.</figcaption>
    </figure>
  );
}

function Headlines() {
  return (
    <div className="benchmark-headlines" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 0, border: '1px solid var(--line)' }}>
      {HEADLINES.map((h, i) => (
        <div key={h.label} style={{
          padding: '40px 32px',
          borderRight: i < HEADLINES.length - 1 ? '1px solid var(--line)' : 'none',
          display: 'flex', flexDirection: 'column',
          background: i === 0 ? 'var(--ink-2)' : 'transparent',
        }}>
          <div style={{ fontFamily: "'Inter Tight', sans-serif", color: 'var(--stone-2)', fontSize: 13.5, letterSpacing: '0.005em', marginBottom: 18 }}>{h.label}</div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, marginBottom: 10 }}>
            <span style={{ fontFamily: "'Inter Tight', sans-serif", fontSize: 'clamp(44px, 5vw, 64px)', fontWeight: 400, color: i === 0 ? 'var(--bronze)' : 'var(--fg)', letterSpacing: '-0.02em', lineHeight: 1 }}>
              {h.val}
            </span>
            <span style={{ fontFamily: "'Inter Tight', sans-serif", fontSize: 22, fontWeight: 400, color: 'var(--stone-2)' }}>{h.unit}</span>
          </div>
          <div style={{ fontFamily: "'Inter Tight', sans-serif", color: 'var(--stone)', fontSize: 13, lineHeight: 1.5 }}>{h.sub}</div>
        </div>
      ))}
    </div>
  );
}

function BenchmarksPage() {
  useFadeUpReveal();
  return (
    <>
      <Nav current="benchmarks" />

      {/* Hero */}
      <section className="section">
        <div className="container">
          <div>
            <h1 className="t-display fade-up in" style={{ margin: '32px 0 24px', maxWidth: 1100 }}>
              Editing a live KV cache, measured against a full re-ingest.
            </h1>
            <p className="t-body-lg fade-up delay-1 in" style={{ maxWidth: 760, marginBottom: 48 }}>
              Splice deletes, replaces and reorders spans inside a cached sequence and repairs only the computation the edit touches. Every number here is wall-clock against re-prefilling the same content on the same GPU, and every row passed a correctness gate calibrated on a fresh re-ingest: pooled p95 teacher-forced <span className="mono">KL &lt; 0.05</span>.
            </p>
          </div>
        </div>
      </section>

      {/* Headline stats */}
      <section className="section" style={{ borderTop: '1px solid var(--line)', paddingTop: 40 }}>
        <div className="container">
          <Headlines />
          <div style={{ fontFamily: "'Inter Tight', sans-serif", color: 'var(--stone)', fontSize: 13, marginTop: 16 }}>
            1–2× H100 · Qwen2.5 / Qwen3-VL / Voxtral · Aug–Sep 2026
          </div>
        </div>
      </section>

      {/* How we measure */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Definitions, before the numbers.</h2>
            <p className="t-body">
              Speed-ups on inference systems are easy to inflate, so every result on this page uses these definitions.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <dl className="splice-defs fade-up">
              <dt>Speed-up</dt><dd>Wall-clock time of the edit divided into the time to re-prefill the same content from the edit point, on the same GPU and attention kernels. Media results also give the ratio against a full re-ingest including the encoder.</dd>
              <dt>Per edit</dt><dd>Each edit after the first, with the one-time setup pass excluded. This is the steady-state cost.</dd>
              <dt>All-in</dt><dd>The whole session, with the initial prefill and setup charged to every method.</dd>
              <dt>Quality</dt><dd>Teacher-forced KL divergence between next-token distributions from the edited cache and from a fresh re-ingest of the edited input, pooled over hundreds of probe positions, 95th percentile. A result passes below 0.05.</dd>
              <dt>Control</dt><dd>A recompute-everything arm runs alongside every experiment and must read approximately zero, so a broken measurement cannot pass.</dd>
            </dl>
          </div>
        </div>
      </section>

      {/* Text results */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Per-edit speed-up holds as context grows.</h2>
            <p className="t-body">
              On a single H100, per-edit speed-up is flat from 8k to 128k tokens. Both Splice's edit and the re-prefill it replaces grow with context, so the ratio holds. All-in figures charge the one-time setup pass to every edit, which is the cost to beat on smaller models.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <ContextChart />
            <div className="splice-table-wrap fade-up">
              <table className="splice-table">
                <caption><b>Speed-up by model size</b><span>8k context · 10 edits · vs re-prefilling from the edit point</span></caption>
                <thead><tr><th>Model</th><th>Hardware</th><th className="n">Per edit</th><th className="n">All-in</th><th className="n">p95 KL</th></tr></thead>
                <tbody>
                  <tr><td>Qwen2.5-7B-Instruct-1M, 15% budget</td><td>1× H100</td><td className="n">1.50×</td><td className="n">0.85×</td><td className="n">0.0005</td></tr>
                  <tr><td>Qwen2.5-14B, 15% budget</td><td>1× H100</td><td className="n">1.98×</td><td className="n">0.93×</td><td className="n">0.0018</td></tr>
                  <tr><td>Qwen2.5-72B-Instruct, 15% budget</td><td>2× H100</td><td className="n">1.94×</td><td className="n">1.30×</td><td className="n">0.0015</td></tr>
                  <tr><td>Qwen2.5-72B-Instruct, 2% budget</td><td>2× H100</td><td className="n hi">3.13×</td><td className="n hi">1.65×</td><td className="n">0.0084</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      {/* Head to head */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Against other ways to reuse a cache after an edit.</h2>
            <p className="t-body">
              On twelve real OpenHands agent sessions per cell, rival methods reimplemented in our harness and run under the same quality gate, each at its smallest passing recompute fraction. Splice leads on every deletion size we tested.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <HeadToHeadChart />
          </div>
        </div>
      </section>

      {/* Inside SGLang */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Splice as an SGLang radix-cache backend.</h2>
            <p className="t-body">
              A standalone harness is not a server. We built Splice into SGLang, with edits arriving over a socket and repair batched by block. Ranking cost is charged inside every edit. Block-batched repair is byte-identical to repairing row by row.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <div className="splice-stats fade-up">
              <div className="splice-stat"><b>2.75×</b><span>faster than SGLang re-prefilling the edited sequence</span></div>
              <div className="splice-stat"><b>25%</b><span>of a 16k context deleted, over 12 real agent trajectories</span></div>
              <div className="splice-stat"><b>0.0485</b><span>p95 KL, under the 0.05 threshold; full-recompute control reads 0.0</span></div>
            </div>
          </div>
        </div>
      </section>

      {/* Multimodal */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Skipping the encoder is where the large ratios come from.</h2>
            <p className="t-body">
              The same edit works on multimodal caches. When nothing needs repairing, the edit is just a cut and a re-rotation, measured in milliseconds, while a re-ingest must run the vision or audio encoder again. The second column removes the encoder from the baseline, since some engines cache encoder output.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <div className="splice-table-wrap fade-up">
              <table className="splice-table">
                <caption><b>Multimodal edits</b><span>1× H100 · each result passed the pooled quality check</span></caption>
                <thead><tr><th>Edit</th><th>Model</th><th className="n">vs full re-ingest</th><th className="n">vs prefill only</th></tr></thead>
                <tbody>
                  <tr><td>Delete 20% of a 10-minute video</td><td>Qwen2.5-VL-7B</td><td className="n hi">185–219×</td><td className="n">~67×</td></tr>
                  <tr><td>Delete 40% of a video (16 films, median)</td><td>Qwen3-VL-8B</td><td className="n hi">4.6×</td><td className="n">3.2×</td></tr>
                  <tr><td>Swap two spans, 97k tokens</td><td>Qwen2.5-VL-7B</td><td className="n hi">3.55×</td><td className="n">2.12×</td></tr>
                  <tr><td>Remove 1 of 16 images</td><td>Qwen2.5-VL-7B</td><td className="n hi">63×</td><td className="n">n/a</td></tr>
                  <tr><td>Remove 1 of 8 images</td><td>Qwen2.5-VL-7B</td><td className="n hi">3.1×</td><td className="n">n/a</td></tr>
                  <tr><td>Delete 16% of a recording</td><td>Voxtral-Mini-3B</td><td className="n hi">66–76×</td><td className="n">35–43×</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      {/* Correctness */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 760, marginBottom: 40 }}>
            <h2 className="t-h2" style={{ margin: '24px 0 20px' }}>Every row on this page cleared the gate.</h2>
            <p className="t-body">
              An edit is only served if it certifies against a fresh re-ingest. Pooled p95 teacher-forced KL under 0.05 passes; anything above falls back to a normal re-prefill.
            </p>
          </div>
          <div style={{ maxWidth: 900 }}>
            <QualityChart />
          </div>
        </div>
      </section>

      {/* Caveats / scope */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container" style={{ maxWidth: 820 }}>
          <h2 className="t-h2" style={{ margin: '24px 0 28px' }}>
            <em style={{ fontStyle: 'italic' }}>Where these numbers hold, and where they don't.</em>
          </h2>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 18 }}>
            {[
              ['Setup is the cost to beat on small models.', 'Per-edit gains hold at every context length, but at 7B and 14B a session needs 183–302 edits before the one-time setup pass is repaid. Cutting that pass is the main engineering target.'],
              ['The SGLang numbers are single-tenant.', 'Under co-tenant traffic the edit path failed KV admission on 74 / 78 / 82% of attempts at 4 / 8 / 16 streams. Admission-aware scheduling is next.'],
              ['The runtime threshold is conservative.', 'On held-out 14B edits it certified 34% of safe edits at 0.949 coverage; the rest fall back to re-prefill.'],
              ['Baselines were reimplemented.', 'InfoFlow, CacheBlend and the seam-repair rivals ran as ports inside our harness under one quality check, not from their released code.'],
              ['Results pass on pooled p95, not worst-case.', 'On the hardest single probe, some video and text cells need more repair to pass. Each model also needs its own exactness test, since M-RoPE layouts differ.'],
            ].map(([h, b], i) => (
              <li key={i} style={{ display: 'flex', gap: 16, paddingBottom: 16, borderBottom: '1px solid var(--line)' }}>
                <span className="t-mono" style={{ color: 'var(--bronze)', flexShrink: 0, width: 24 }}>0{i+1}</span>
                <div>
                  <div className="serif" style={{ fontSize: 20, fontWeight: 400, marginBottom: 6 }}>{h}</div>
                  <div className="t-body" style={{ color: 'var(--stone-2)', fontSize: 14 }}>{b}</div>
                </div>
              </li>
            ))}
          </ul>
          <p className="t-mono fade-up" style={{ color: 'var(--stone)', fontSize: 11, lineHeight: 1.7, marginTop: 36 }}>
            Measured August–September 2026 on H100 GPUs (1–2 per run) with Hugging Face Transformers, PyTorch and SGLang. Quality gate specified before measurement. Models: Qwen2.5-7B-Instruct-1M, Qwen2.5-14B, Qwen2.5-72B-Instruct, Qwen2.5-VL-7B, Qwen3-VL-8B, Voxtral-Mini-3B. Offline calibration is not charged in any ratio.
          </p>
        </div>
      </section>

      {/* Final CTA */}
      <section className="section section--tight" style={{ background: 'var(--ink-2)', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 720 }}>
            <h2 className="t-h2" style={{ marginTop: 20, marginBottom: 16 }}>Long, frequently edited contexts?</h2>
            <p className="t-body-lg" style={{ marginBottom: 32 }}>
              If you run self-hosted open models on SGLang or vLLM with contexts that change every few turns, we'll benchmark Splice on your workload and share the certificate numbers.
            </p>
            <Button variant="primary" href={CALENDLY} target="_blank" className="hero-btn hero-cta">Book a call</Button>
          </div>
        </div>
      </section>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<BenchmarksPage />);
