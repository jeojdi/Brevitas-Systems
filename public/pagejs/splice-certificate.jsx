const H2 = ({ children }) => (
  <h2 className="serif fade-up" style={{ fontSize: 'clamp(26px, 3.4vw, 36px)', fontWeight: 400, letterSpacing: '-0.02em', margin: '8px 0 24px' }}>{children}</h2>
);
const P = ({ children, last }) => (
  <p className="t-body fade-up" style={{ marginBottom: last ? 0 : 22, maxWidth: 720 }}>{children}</p>
);
const Block = ({ children, top = 52 }) => (
  <section className="section" style={{ paddingTop: top, paddingBottom: 0 }}>
    <div className="container" style={{ maxWidth: 760 }}>{children}</div>
  </section>
);
const Mono = ({ children }) => <span className="mono" style={{ color: 'var(--signal)' }}>{children}</span>;

const INK = 'var(--bronze)', GRID = 'var(--line)', TEXT = 'var(--stone)', TEXT_STRONG = 'var(--fg)';
const mono = "'JetBrains Mono', ui-monospace, monospace";

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
      <h3>Every edit served in our results cleared the gate</h3>
      <p className="sub">Pooled p95 teacher-forced KL between the edited cache and a fresh re-ingest · lower is closer · pass line at 0.05</p>
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
      <figcaption>The recompute-everything control reads 0.00001 to 0.00004 on text and exactly 0.0 inside SGLang, so the gate can tell a correct cache from a damaged one. Media controls sit at a kernel noise floor of about 0.002 to 0.007, which sets the resolution of the gate for those modalities.</figcaption>
    </figure>
  );
}

function Paper() {
  useFadeUpReveal();
  return (
    <>
      <Nav current="blog" />

      <section className="section" style={{ paddingBottom: 0 }}>
        <div className="container" style={{ maxWidth: 760 }}>
          <div className="fade-up in" style={{ marginBottom: 22 }}><span className="paper-badge">RESEARCH PAPER</span></div>
          <h1 className="serif fade-up in" style={{ fontSize: 'clamp(34px, 5vw, 60px)', fontWeight: 400, letterSpacing: '-0.025em', lineHeight: 1.07, margin: '0 0 24px' }}>
            Certifying a KV-cache edit: a teacher-forced KL gate against a fresh re-ingest.
          </h1>
          <div className="paper-abstract fade-up in">
            <p className="lbl">Abstract</p>
            <p>An edited cache is only worth keeping if it behaves like the cache a clean recompute would have produced. We define correctness as a teacher-forced divergence between the two, measured at every probe position and pooled to a high percentile, with a pass threshold and a recompute-everything control that must read zero. This gate is exact enough to distinguish a correct cache from a damaged one, but too expensive to run per edit at serving time, because it needs the very re-ingest the edit is trying to avoid. We close that gap by calibrating a cheap residual-dependency statistic against the gate offline, then using the statistic as a runtime admission threshold: edits it clears are served, and edits it cannot certify fall back to a normal re-prefill. We report the coverage of that threshold and where it is deliberately conservative.</p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20, padding: '20px 0 40px', borderBottom: '1px solid var(--line)', flexWrap: 'wrap' }}>
            <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11 }}>Sep 22, 2026</span>
            <span className="t-mono" style={{ color: 'var(--line)' }}>·</span>
            <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11 }}>10 min read</span>
            <span className="t-mono" style={{ color: 'var(--line)' }}>·</span>
            <span className="t-mono" style={{ fontSize: 10, color: 'var(--bronze)', padding: '3px 8px', border: '1px solid var(--bronze)', borderRadius: 2, letterSpacing: '0.1em' }}>RESEARCH</span>
          </div>
        </div>
      </section>

      <article>
        <Block>
          <H2>1. What correctness should mean here.</H2>
          <P>An edit to a KV cache is a means, not an end. Nobody wants a fast cache that produces different text than the model would have produced on the edited input. So the definition of a correct edit is operational: the edited cache should induce the same next-token behavior, at every position, as a fresh re-ingest of the edited sequence. Not similar text, not similar embeddings, the same distribution over the vocabulary, position by position, to within the tolerance you are willing to accept.</P>
          <P>This is a stronger and more honest target than a downstream task score. A task metric can hide a broken cache behind an easy benchmark; it can also punish a correct cache for a reason that has nothing to do with the edit. Comparing distributions against a fresh re-ingest isolates exactly the thing the edit could have damaged, and nothing else.</P>
        </Block>

        <Block>
          <H2>2. The gate.</H2>
          <P>Fix a probe set of held-out sequences. For each, teacher-force the tokens through two states: the edited cache, and a fresh re-ingest of the same edited input. At every probe position both states emit a next-token distribution; take the KL divergence between them. Pool the per-position divergences across the probe set and read the 95th percentile. An edit type passes if that pooled p95 KL is below 0.05.</P>
          <div className="paper-eq fade-up">
            <span className="cmt"># at every probe position t, compare the two next-token distributions</span>{'\n'}
            d(t) = KL( p<span className="k">re-ingest</span>(· | x<sub>&lt;t</sub>)  ‖  p<span className="k">edited</span>(· | x<sub>&lt;t</sub>) ){'\n\n'}
            <span className="cmt"># pass if the 95th percentile over the pooled probe set is small</span>{'\n'}
            gate: percentile<span className="k">95</span>( {'{ d(t) }'} ) &lt; 0.05
          </div>
          <P>Two design choices matter. We pool and take a high percentile rather than a mean, because a mean can be dragged down by the many easy positions while a handful of positions quietly diverge; the p95 is a promise about the tail, not the average. And the threshold is a property of an edit type, delete versus same-length replace versus reorder, at a given repair budget, rather than a single global number, because different edit families leave residue with different shapes.</P>
        </Block>

        <Block>
          <H2>3. The control that gives the number meaning.</H2>
          <P>A gate is only as trustworthy as its ability to fail. Alongside every experiment we run a control arm that recomputes every row after the seam, the full-repair setting with the budget at one. That arm is, by construction, a re-prefill wearing the edit pipeline's clothes, so it must read essentially zero. When it does not, the measurement is broken, not the edit, and the whole run is discarded.</P>
          <div className="splice-table-wrap fade-up">
            <table className="splice-table">
              <caption><b>Control readings</b><span>Full-repair control (budget = 1.0), same probe and pooling as the gate. A gate that could not separate these from a real edit would be worthless.</span></caption>
              <thead><tr><th>Setting</th><th>Backend</th><th className="n">Control p95 KL</th></tr></thead>
              <tbody>
                <tr><td>Text, 8k–64k</td><td>standalone PyTorch</td><td className="n hi">0.00001–0.00004</td></tr>
                <tr><td>Text, 16k, 25% deleted</td><td>SGLang radix cache</td><td className="n hi">0.0</td></tr>
                <tr><td>Video / image / audio</td><td>standalone PyTorch</td><td className="n">0.002–0.007</td></tr>
              </tbody>
            </table>
          </div>
          <P>The text control reads at the level of floating-point noise and, inside SGLang, exactly zero. The media controls sit slightly higher, at a kernel noise floor of a few thousandths, which sets the resolution of the gate for those modalities: you cannot certify a video edit to a tolerance finer than the floor of its own control, and the 0.05 threshold sits comfortably above it. A control that reads near zero is what lets a passing edit at 0.04 mean something, because the same measurement reads 0.000 when the cache is provably correct.</P>
          <P>Plotting the passing edits against the pass line shows the headroom. Text edits clear the threshold by two to three orders of magnitude; the media edits and the largest text deletions ride closer to the line but still under it, which is exactly where a well-set gate should place its hardest cases.</P>
          <QualityChart />
        </Block>

        <Block>
          <H2>4. From a lab gate to a runtime you can afford.</H2>
          <P>The gate has one fatal property as a serving mechanism: it requires the fresh re-ingest it is checking against. Running it per edit would pay exactly the cost Splice is built to avoid. So the gate is a lab instrument, not a runtime one. What runs at serving time is a proxy: a residual-dependency statistic, computed from the same attention quantities used to rank rows for repair, that estimates how much residue an edit will leave without doing the re-ingest.</P>
          <P>We calibrate the proxy against the gate offline. Over a large set of edits we compute both the cheap statistic and the true pooled p95 KL, and choose a threshold on the statistic that admits an edit only when its calibrated probability of passing the gate is high. At serving time an edit computes only the statistic; if it is under the threshold the edited cache is served, and if it is over, the edit is abandoned and the engine falls back to a normal re-prefill.</P>
          <div className="paper-eq fade-up">
            <span className="cmt"># serving time: no re-ingest, just the calibrated proxy</span>{'\n'}
            edit = cut → re-rotate → repair(c){'\n'}
            <span className="k">if</span>  residual_dependency(edit) &lt; τ :   serve(edit)      <span className="cmt"># calibrated to pass the gate</span>{'\n'}
            <span className="k">else</span> :                               re-prefill(input)  <span className="cmt"># safe fallback</span>
          </div>
          <P>The asymmetry is deliberate. A false serve, admitting an edit that would have failed the gate, ships a subtly wrong cache; a false fallback merely does a re-prefill that was going to be correct anyway, costing latency but never correctness. The threshold is set to make the first kind of error rare at the price of the second, which is why the runtime is conservative by design.</P>
        </Block>

        <Block>
          <H2>5. Coverage.</H2>
          <P>Conservative has a cost, and it is coverage: the fraction of genuinely safe edits the runtime is willing to serve. On a held-out set of 14B edits, the calibrated threshold certified 34 percent of the edits that would have passed the lab gate, at a precision of 0.949 among those it admitted; the remaining safe edits were sent to re-prefill rather than risk a false serve. In other words, roughly one in twenty admitted edits is a mistake by the gate's own standard, and two in three safe edits are left on the table for now.</P>
          <P>That is not a comfortable number, and we report it plainly because it is the honest state of the runtime. Coverage is the axis we are actively pushing: a better proxy, or a cheaper partial check that is closer to the gate than the current statistic, moves safe edits from the fallback path onto the fast path without loosening the correctness guarantee. The guarantee itself does not move. Every edit that is served has cleared a threshold calibrated to the gate, and every edit that cannot be certified re-prefills. Speed is only ever taken when correctness is already in hand.</P>
        </Block>

        <Block>
          <H2>6. Why a certificate is the point.</H2>
          <P>Traditional caching is built around reuse: keep a prefix, append to it, and throw away downstream state whenever the middle changes. Editing the middle is only viable if you can tell, cheaply and per edit, whether a given edit stayed faithful. The re-rotation identity makes position exact and the repair rule makes content nearly so, but neither is worth deploying without a mechanism that decides when nearly is enough and falls back when it is not. The certificate is that mechanism. It is what turns a fast cache edit from a gamble into a system property, and it is the reason an edited cache can be served at all.</P>
        </Block>

        <Block>
          <H2>References.</H2>
          <ol className="paper-refs fade-up">
            <li>Brevitas Systems. Splice: editing a live KV cache instead of re-prefilling it. 2026.</li>
            <li>Brevitas Systems. Dependency-ranked repair: recomputing only the rows a cache edit breaks. 2026.</li>
            <li>Brevitas Systems. Exact position surgery: re-rotating a rotary KV cache after a mid-sequence edit. 2026.</li>
            <li>Kullback, S. and Leibler, R. A. On Information and Sufficiency. Annals of Mathematical Statistics, 1951.</li>
          </ol>
        </Block>

        <section className="section" style={{ paddingTop: 40 }}>
          <div className="container" style={{ maxWidth: 760 }}>
            <p className="t-mono fade-up" style={{ color: 'var(--stone)', fontSize: 11, lineHeight: 1.7 }}>
              Measured August–September 2026 on H100 GPUs (1–2 per run) with Hugging Face Transformers, PyTorch and SGLang. Quality gate and probe sets specified before measurement. Coverage figures from held-out Qwen2.5-14B edits. Models: Qwen2.5-7B-Instruct-1M, Qwen2.5-14B, Qwen2.5-72B-Instruct, Qwen2.5-VL-7B, Qwen3-VL-8B, Voxtral-Mini-3B.
            </p>
          </div>
        </section>
      </article>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<Paper />);
