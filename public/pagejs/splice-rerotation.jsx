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

const INK = 'var(--bronze)', RIVAL_A = 'var(--stone)', GRID = 'var(--line)', TEXT = 'var(--stone)', TEXT_STRONG = 'var(--fg)';
const mono = "'JetBrains Mono', ui-monospace, monospace";

const REROT = [
  { label: 'Re-rotate (Splice)', kl: 0.00003, over: false },
  { label: 'Shift-only reuse', kl: 0.041, over: false },
  { label: 'No correction', kl: 0.06, over: true },
];
function RerotChart() {
  const W = 680, rowH = 48, L = 160, R = 42, T = 8;
  const H = T + REROT.length * rowH + 30;
  const max = 0.06;
  const x = v => L + (Math.min(v, max) / max) * (W - L - R);
  return (
    <figure className="splice-fig fade-up">
      <h3>Position correction, compared</h3>
      <p className="sub">Pooled p95 teacher-forced KL after a mid-sequence delete, before any content repair · lower is closer to a fresh prefill · pass line at 0.05</p>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Bar chart of KL for position-correction strategies: re-rotate 0.00003, shift-only 0.041, no correction above 0.06.">
        {[0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06].map(v => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={T} y2={H - 26} style={{ stroke: GRID }} strokeWidth={v === 0.05 ? 2 : 1} strokeDasharray={v === 0.05 ? '5 4' : ''} />
            <text x={x(v)} y={H - 10} textAnchor="middle" style={{ fill: v === 0.05 ? TEXT_STRONG : TEXT, fontFamily: mono, fontSize: 11 }}>{v === 0.05 ? '0.05 pass' : v.toFixed(2)}</text>
          </g>
        ))}
        {REROT.map((d, i) => (
          <g key={d.label}>
            <text x={L - 12} y={T + i * rowH + 23} textAnchor="end" style={{ fill: TEXT_STRONG, fontSize: 12.5 }}>{d.label}</text>
            <rect x={L} y={T + i * rowH + 11} width={Math.max(x(d.kl) - L, 2)} height="16" rx="3" style={{ fill: i === 0 ? INK : RIVAL_A }} />
            <text x={x(d.kl) + 8} y={T + i * rowH + 23} style={{ fill: TEXT, fontFamily: mono, fontSize: 11 }}>{d.over ? '>0.06' : (d.kl < 0.001 ? d.kl.toFixed(5) : d.kl.toFixed(3))}</text>
          </g>
        ))}
      </svg>
      <figcaption>Re-rotation is the only correction that reaches the floating-point floor. A naive shift that reuses keys without rotating lands near the pass line and fails outright on larger deletes, and skipping position correction entirely diverges off the chart. Content repair, covered in the companion note, closes the remaining gap for mid-sequence edits.</figcaption>
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
            Exact position surgery: re-rotating a rotary KV cache after a mid-sequence edit.
          </h1>
          <div className="paper-abstract fade-up in">
            <p className="lbl">Abstract</p>
            <p>A rotary cache bakes each token's absolute position into its key by a fixed rotation. Deleting a span shifts every later token's position, which naively invalidates the cache. We show that the correction is a single rotation applied to each surviving key, that it is exact rather than approximate because rotations compose, and that it extends coordinate-wise to the multi-axis rotary schedules used by vision-language models. We give the identity, a short proof, the multi-axis form, and a control experiment in which a re-rotated cache reproduces a fresh prefill to within floating-point noise.</p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20, padding: '20px 0 40px', borderBottom: '1px solid var(--line)', flexWrap: 'wrap' }}>
            <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11 }}>Sep 18, 2026</span>
            <span className="t-mono" style={{ color: 'var(--line)' }}>·</span>
            <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11 }}>9 min read</span>
            <span className="t-mono" style={{ color: 'var(--line)' }}>·</span>
            <span className="t-mono" style={{ fontSize: 10, color: 'var(--bronze)', padding: '3px 8px', border: '1px solid var(--bronze)', borderRadius: 2, letterSpacing: '0.1em' }}>RESEARCH</span>
          </div>
        </div>
      </section>

      <article>
        <Block>
          <H2>1. The append-only cache.</H2>
          <P>A transformer serving a long prompt stores, for every token at every layer, a key and a value. That store, the KV cache, is what makes each next token cheap: attention reads it instead of recomputing it. Two properties make it hard to edit in the middle. First, each key carries its absolute position, written in by a rotary rotation at the moment it was computed. Second, every later token's key and value were produced while attending to the tokens before it, so they carry traces of anything you might want to remove.</P>
          <P>This paper addresses the first property in isolation. Suppose we delete a contiguous span of width <Mono>w</Mono> that ends before a surviving token at position <Mono>m</Mono>. After the deletion that token should sit at position <Mono>m − w</Mono>. Its value is position-free and needs nothing. Its key was stored as if it lived at position <Mono>m</Mono>. The question is what, if anything, must change so the key behaves as though it had always lived at <Mono>m − w</Mono>. The answer is a single rotation, and it is exact. The second property, residue from attention over the deleted tokens, is the subject of a companion paper on dependency-ranked repair.</P>
        </Block>

        <Block>
          <H2>2. Rotary position, in one paragraph.</H2>
          <P>Rotary position embedding [1] does not add a position vector; it rotates. The head dimension is split into two-dimensional planes, and the plane at frequency <Mono>θ</Mono> for a token at position <Mono>m</Mono> is turned by angle <Mono>mθ</Mono>. Write the block-diagonal rotation for position <Mono>m</Mono> as <Mono>R(m)</Mono>. A query at position <Mono>m</Mono> becomes <Mono>R(m)q</Mono> and a key at position <Mono>n</Mono> becomes <Mono>R(n)k</Mono>, so their dot product depends only on the difference:</P>
          <div className="paper-eq fade-up">
            (R(m)q)ᵀ (R(n)k) = qᵀ R(m)ᵀ R(n) k = qᵀ R(n − m) k
          </div>
          <P>The two facts we need both live in that line. Rotations are orthogonal, so <Mono>R(m)ᵀ = R(−m)</Mono>. And they compose additively in the angle, so <Mono>R(a)R(b) = R(a + b)</Mono>. Attention sees only relative position because the absolute rotations cancel. That is exactly the lever an editor wants.</P>
        </Block>

        <Block>
          <H2>3. The re-rotation identity.</H2>
          <P>The stored key for the surviving token is <Mono>k̂ = R(m)k</Mono>, where <Mono>k</Mono> is the raw projection the model produced before position was applied. We never kept <Mono>k</Mono>; we kept <Mono>k̂</Mono>. We want the key that a fresh prefill of the edited sequence would have written, namely <Mono>R(m − w)k</Mono>. Using additive composition:</P>
          <div className="paper-eq fade-up">
            <span className="cmt"># stored key at position m</span>{'\n'}
            k̂ = R(m) · k{'\n\n'}
            <span className="cmt"># rotate it back by the cut width w</span>{'\n'}
            R(−w) · k̂ = R(−w) · R(m) · k = R(m − w) · k{'\n\n'}
            <span className="cmt"># which is exactly the fresh-prefill key at position m − w</span>
          </div>
          <P>So the entire correction for a delete of width <Mono>w</Mono> is: multiply every surviving key after the cut by the single rotation <Mono>R(−w)</Mono>. No raw projections are needed, no per-token angle bookkeeping beyond the shared shift, and nothing touches the values. Because the identity is an equality and not a bound, the re-rotated key is bit-for-bit the fresh-prefill key up to the order of floating-point operations, not an approximation of it. Reordering two spans is the same statement applied piecewise: each surviving run is shifted by the signed change in the number of tokens before it.</P>
          <P>The one region where this is not allowed is the very start of the sequence. Attention sinks [2], the first few tokens that absorb a large share of probability mass, must keep their positions; a cut that lands on them changes what every downstream query attends to in a way a rotation cannot repair. Splice forbids edits that touch the sink prefix.</P>
        </Block>

        <Block>
          <H2>4. Multi-axis caches: M-RoPE.</H2>
          <P>Vision-language models do not lay position out on a single line. Qwen2.5-VL and Qwen3-VL use multi-axis rotary position (M-RoPE), which assigns each token a tuple of coordinates, a temporal index and two spatial indices for image and video tokens, and rotates a disjoint set of head-dimension planes by each coordinate independently [3]. The single scalar <Mono>m</Mono> becomes a vector, and <Mono>R(m)</Mono> becomes a product of per-axis rotations acting on disjoint planes.</P>
          <P>Because the axes act on disjoint coordinates, the composition identity holds per axis. Editing a span recomputes the position tuple for every surviving token under the edited layout, and each axis is corrected by its own delta:</P>
          <div className="paper-eq fade-up">
            <span className="cmt"># M-RoPE: recompute position ids for the edited sequence,</span>{'\n'}
            <span className="cmt"># then rotate each (t, h, w) section by its own delta</span>{'\n'}
            k̂' = ∏<span className="k">a ∈ {'{t,h,w}'}</span>  R<span className="k">a</span>(−Δ<span className="k">a</span>) · k̂
          </div>
          <P>Deleting a two-second clip from a video shifts the temporal axis for everything after it, and typically leaves the two spatial axes unchanged; deleting one image from a grid of sixteen shifts a different combination. The delta is whatever the edited layout dictates, computed once per axis and applied as a rotation. Nothing here is model-agnostic, however: Qwen3-VL and Qwen2.5-VL differ in how they number and interleave the axes, so the mapping from an edit to the per-axis deltas has to be derived and tested per model. This is the single largest source of engineering care in the re-rotation stage.</P>
        </Block>

        <Block>
          <H2>5. Does it actually reproduce a prefill?</H2>
          <P>The identity is exact on paper. Whether an implementation realizes it is an empirical question, because a real system applies the rotations in fused kernels, in a different order from the original prefill, and in reduced precision. We test it with a control: take the edited input, run a full fresh re-ingest to get ground-truth key and value tensors, run Splice's cut-and-re-rotate to get the edited cache, and compare next-token distributions position by position under teacher forcing, reporting the pooled 95th-percentile KL divergence. A re-rotation that were merely close would show up here as a nonzero floor.</P>
          <div className="splice-table-wrap fade-up">
            <table className="splice-table">
              <caption><b>Re-rotate-only control, no repair</b><span>Pooled p95 teacher-forced KL between the edited cache and a fresh re-ingest of the same edited input. Delete at the sequence boundary, where no later row carries residue.</span></caption>
              <thead><tr><th>Model</th><th>Axes</th><th>Edit</th><th className="n">p95 KL</th></tr></thead>
              <tbody>
                <tr><td>Qwen2.5-7B-Instruct-1M</td><td>1-D RoPE</td><td>delete 5% at the tail</td><td className="n hi">0.00001</td></tr>
                <tr><td>Qwen2.5-14B</td><td>1-D RoPE</td><td>delete 5% at the tail</td><td className="n hi">0.00003</td></tr>
                <tr><td>Qwen2.5-VL-7B</td><td>M-RoPE</td><td>remove 1 of 16 images</td><td className="n">0.0</td></tr>
                <tr><td>Qwen3-VL-8B</td><td>M-RoPE</td><td>delete a 2s video span</td><td className="n">0.0</td></tr>
              </tbody>
            </table>
          </div>
          <P>The text models read a KL at the level of fp16 rounding noise; the vision models, whose image and video tokens sit at boundaries that avoid mid-token residue, read exactly zero on the probe set. This is the control that gives the rest of the system its meaning: a measurement that could not distinguish a correct cache from a damaged one would be worthless, and this one bottoms out precisely where the theory says it should.</P>
          <P>It also separates re-rotation from the cheaper things one might try instead. Reusing a shifted cache without rotating, the strategy behind llama.cpp-style context reuse and several published position-invariance tricks, leaves each key at the wrong angle; on a mid-sequence delete that lands near the pass line and fails as the deletion grows. Skipping position correction altogether diverges immediately. The figure places the three side by side on the same probe set.</P>
          <RerotChart />
        </Block>

        <Block>
          <H2>6. What re-rotation does not fix.</H2>
          <P>Boundary deletes are the easy case precisely because nothing after the cut ever attended to the deleted tokens. A mid-sequence delete is different: keys and values downstream of the seam were computed while attending to the span you removed, and re-rotation, which only corrects position, leaves that content residue untouched. Re-rotation is necessary and exact for position; it is not sufficient for content. The residue is small and concentrated, which is why recomputing a ranked two-to-twenty percent of the downstream rows is enough to certify a mid-sequence edit. That is the subject of the companion note on repair, and the certificate that decides when repair has done enough is the subject of the note after it.</P>
        </Block>

        <Block>
          <H2>7. Fitting the serving path.</H2>
          <P>Re-rotation is attractive to deploy because it is almost free and entirely local. A rotation touches each surviving key once, costs on the order of the number of shifted tokens times the head dimension, and reads and writes the same cache slots in place. There is no gather, no extra forward, and no dependence on the raw pre-position projections, which serving engines do not keep. In Splice's SGLang radix-cache backend the shift is applied over the paged cache as the first stage of an edit, ahead of repair, and for multi-axis models the per-axis deltas are computed once from the edited position layout and fused into the same pass.</P>
          <P>Because the correction is exact and cheap, it sets the requirements for everything downstream. An edit path needs direct KV-cache access, which means self-hosted open models on SGLang or vLLM or a platform that hosts them; a standard RoPE or M-RoPE schedule fixed at serving time; fp16, bf16 or int8 keys; and, for any model whose M-RoPE layout has not been verified, a per-model exactness test of the kind reported above before an edit type is enabled in production. Those are modest constraints, and they are the price of turning a write-once cache into one you can operate on. Where re-rotation ends and content repair begins is where the interesting engineering starts, and it is measured end to end on the <a href="/benchmarks" className="link">benchmarks page</a>.</P>
        </Block>

        <Block>
          <H2>References.</H2>
          <ol className="paper-refs fade-up">
            <li>Su, J. et al. RoFormer: Enhanced Transformer with Rotary Position Embedding. 2021.</li>
            <li>Xiao, G. et al. Efficient Streaming Language Models with Attention Sinks. 2023.</li>
            <li>Qwen Team. Qwen2.5-VL and Qwen3-VL Technical Reports: multi-axis rotary position for interleaved image, video and text. 2025.</li>
            <li>Brevitas Systems. Splice: editing a live KV cache instead of re-prefilling it. 2026.</li>
          </ol>
        </Block>

        <section className="section" style={{ paddingTop: 40 }}>
          <div className="container" style={{ maxWidth: 760 }}>
            <p className="t-mono fade-up" style={{ color: 'var(--stone)', fontSize: 11, lineHeight: 1.7 }}>
              Controls measured September 2026 on H100 GPUs with Hugging Face Transformers and PyTorch in fp16/bf16. Quality defined as pooled p95 teacher-forced KL divergence against a fresh re-ingest, specified before measurement. Models: Qwen2.5-7B-Instruct-1M, Qwen2.5-14B, Qwen2.5-VL-7B, Qwen3-VL-8B.
            </p>
          </div>
        </section>
      </article>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<Paper />);
