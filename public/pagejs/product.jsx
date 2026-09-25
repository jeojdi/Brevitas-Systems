const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const FEATURES = [
  { h: 'Delete spans', b: 'Cut old tool output, stale screenshots or retrieved docs out of a live cache and close the gap. Every surviving token keeps the exact state the model computed for it.' },
  { h: 'Replace and reorder', b: 'Swap a span for one the same length, or move spans around the sequence. Rotary positions re-rotate exactly, so the edit is bit-for-bit what a fresh prefill would produce.' },
  { h: 'Every modality', b: 'The same edit works on text, video, images and audio, across 1-D RoPE and multi-axis M-RoPE. Skipping the encoder on media is where the largest speed-ups come from.' },
  { h: 'Certified, not hoped', b: 'Each edit is scored against a fresh re-ingest. Safe edits are served; the rest fall back to a clean re-prefill. Correctness is never traded for speed.' },
];

const STEPS = [
  { n: '01', h: 'Cut', b: 'Drop the span’s key and value rows from every layer and close the gap. Cuts that touch the first sink tokens are not allowed, because the rest of the sequence attends to them.' },
  { n: '02', h: 'Re-rotate', b: 'Rotate each surviving key back by the width of the cut. Rotary rotations compose, so the key lands exactly where a fresh prefill would place it. Values carry no position and are left untouched.' },
  { n: '03', h: 'Repair', b: 'Rank the downstream rows by how much they depended on the deleted span, then recompute the top fraction, typically 2 to 20 percent, in one batched pass. Only the rows that actually carry residue are touched.' },
  { n: '04', h: 'Certify', b: 'Teacher-force a probe set through the edited cache and through a fresh re-ingest, and compare next-token distributions. Under the threshold the edit is served; over it, it falls back to a clean re-prefill.' },
];

const MODALITY = [
  ['Delete 20% of a 10-minute video', 'Qwen2.5-VL-7B', '185–219×'],
  ['Delete 40% of a video (median)', 'Qwen3-VL-8B', '4.6×'],
  ['Swap two spans, 97k tokens', 'Qwen2.5-VL-7B', '3.55×'],
  ['Remove 1 of 16 images', 'Qwen2.5-VL-7B', '63×'],
  ['Delete 16% of a recording', 'Voxtral-Mini-3B', '66–76×'],
];

const PAPERS = [
  { h: 'Exact position surgery', href: '/blog/splice-rerotation', img: '/assets/ascii-magic-5-poster.jpg' },
  { h: 'Dependency-ranked repair', href: '/blog/splice-repair', img: '/assets/ascii-magic-4-poster.jpg' },
  { h: 'The correctness certificate', href: '/blog/splice-certificate', img: '/assets/ascii-magic-3-poster.jpg' },
];

function ProductPage() {
  useFadeUpReveal();
  return (
    <>
      <Nav current="product" />

      {/* Hero */}
      <section className="section" style={{ paddingBottom: 0 }}>
        <div className="container">
          <h1 className="fade-up in" style={{ margin: '8px 0 24px', maxWidth: 900, fontFamily: "'Inter Tight', sans-serif", fontWeight: 450, fontSize: 'clamp(40px, 5.4vw, 68px)', letterSpacing: '-0.035em', lineHeight: 1.04, color: 'var(--fg)' }}>
            Splice: edit a model's memory in place.
          </h1>
          <p className="t-body-lg fade-up delay-1 in" style={{ maxWidth: 700, marginBottom: 0, color: 'var(--stone-2)' }}>
            Splice turns a model's KV cache from a write-once buffer into working memory you can edit. Delete a span, replace it, reorder it, exactly and provably, without rebuilding the context from scratch. It is the surgical layer for long-lived AI state.
          </p>
        </div>
      </section>

      {/* Problem */}
      <section className="section">
        <div className="container">
          <p className="prod-label fade-up">Problem</p>
          <h2 className="prod-head fade-up">Every edit to an agent's context throws its memory away.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            When a model reads a prompt, it stores a key and a value for every token at every layer. That KV cache is what makes each next token cheap. It is also append-only: every key has its absolute position baked in by a rotary rotation, and every later token was computed while attending to the ones before it.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            So when an agent trims old tool output, a stale screenshot, or a paragraph that no longer matters, today's serving engines do the safe thing: evict everything from the edit point and prefill it again. The cost scales with everything after the edit, and it is paid again on the very next edit. For vision and audio models, re-ingesting also re-runs the encoder, which is 59 to 75 percent of the cost.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 0 }}>
            Long-running agents edit their context constantly, which means they spend most of their compute rebuilding memory they already had.
          </p>
        </div>
      </section>

      {/* Problem image */}
      <section className="section" style={{ paddingTop: 0 }}>
        <div className="container">
          <img className="prod-img fade-up" src="/assets/product-problem.jpg" alt="Brevitas Systems" loading="lazy" width="1358" height="768" />
        </div>
      </section>

      {/* Solution */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">Our solution</p>
          <h2 className="prod-head fade-up">Splice edits the cache in place, and proves it is correct.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            Splice is KV-cache surgery. Instead of rebuilding context from scratch, it cuts a span out of a live cache, re-rotates the surviving keys into their new positions so the math is exact, and repairs only the small fraction of the computation that actually depended on the change. Then it certifies the edit against a fresh re-ingest before serving it.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 40 }}>
            Most systems throw a model's computed memory away and redo it. Splice edits it directly, exactly, and proves the edit didn't corrupt anything.
          </p>

          <p className="prod-label fade-up" style={{ marginBottom: 24 }}>What Splice does</p>
          <div className="prod-grid fade-up">
            {FEATURES.map((f) => (
              <div key={f.h} className="prod-feature">
                <h3>{f.h}</h3>
                <p>{f.b}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* How an edit runs */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">How an edit runs</p>
          <h2 className="prod-head fade-up" style={{ marginBottom: 8 }}>Four moves on the cache. Nothing else recomputes.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 32 }}>
            Every Splice edit is the same four steps. The first two are exact; the third is where the work is; the fourth is the safety net.
          </p>
          <div className="fade-up">
            {STEPS.map((s) => (
              <div key={s.n} className="prod-step">
                <div className="n">{s.n}</div>
                <div>
                  <h3>{s.h}</h3>
                  <p>{s.b}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Every modality */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">Text, video, image and audio</p>
          <h2 className="prod-head fade-up" style={{ marginBottom: 8 }}>Skipping the encoder is where the large ratios come from.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 28 }}>
            The same edit works on multimodal caches. When nothing needs repairing, the edit is a cut and a re-rotation measured in milliseconds, while a re-ingest has to run the vision or audio encoder again. Every row below cleared the correctness gate.
          </p>
          <div className="prod-table-wrap fade-up">
            <table className="prod-table">
              <thead><tr><th>Edit</th><th>Model</th><th className="n">vs full re-ingest</th></tr></thead>
              <tbody>
                {MODALITY.map(([edit, model, x]) => (
                  <tr key={edit}><td>{edit}</td><td>{model}</td><td className="n hi">{x}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* Correctness */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">Correctness</p>
          <h2 className="prod-head fade-up">Every edit is proven, or it doesn't ship.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            The gate is a teacher-forced KL divergence between the edited cache and a fresh re-ingest, measured at every probe position and pooled to the 95th percentile. An edit passes below 0.05. A recompute-everything control runs alongside and reads essentially zero, so the gate can tell a correct cache from a damaged one.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 0 }}>
            Running that gate per edit would cost the very re-ingest Splice avoids, so at serving time a cheaper residual-dependency proxy, calibrated offline against the gate, decides in real time whether to serve the edit or fall back to a clean re-prefill. You never trade correctness for speed. You get the speed only when correctness is already in hand.
          </p>
        </div>
      </section>

      {/* Measured */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">Measured</p>
          <h2 className="prod-head fade-up" style={{ marginBottom: 20 }}>Up to 12.4× faster edits, every one certified.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 28 }}>
            On real agent sessions, Splice is 9 to 12× faster than re-prefilling the edited context, and 60 to 220× faster on media where it skips the encoder. Every result clears a correctness gate: pooled p95 teacher-forced KL under 0.05 against a fresh re-ingest.
          </p>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <Button variant="ghost" href="/benchmarks" className="hero-btn">See the benchmarks</Button>
            <Button variant="ghost" href="/blog/splice-kv-cache-editing" className="hero-btn" arrow={false}>Read the write-up</Button>
          </div>
        </div>
      </section>

      {/* Research */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">The research</p>
          <h2 className="prod-head fade-up" style={{ marginBottom: 28 }}>The three pillars, in full.</h2>
          <div className="prod-refs fade-up">
            {PAPERS.map((p) => (
              <a key={p.href} href={p.href} className="prod-ref">
                <img src={p.img} alt="" loading="lazy" />
                <div className="k">Research paper</div>
                <h4>{p.h}</h4>
              </a>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="section" style={{ background: 'var(--ink-2)', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 820 }}>
            <h2 className="t-h1" style={{ marginTop: 0, marginBottom: 20 }}>Long, frequently edited contexts?</h2>
            <p className="t-body-lg" style={{ marginBottom: 40, maxWidth: 680 }}>
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

ReactDOM.createRoot(document.getElementById('app')).render(<ProductPage />);
