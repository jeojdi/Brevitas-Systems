const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const FEATURES = [
  { h: 'Delete spans', b: 'Cut old tool output, stale screenshots or retrieved docs out of a live cache and close the gap. Every surviving token keeps the exact state the model computed for it.' },
  { h: 'Replace and reorder', b: 'Swap a span for one the same length, or move spans around the sequence. Rotary positions re-rotate exactly, so the edit is bit-for-bit what a fresh prefill would produce.' },
  { h: 'Every modality', b: 'The same edit works on text, video, images and audio, across 1-D RoPE and multi-axis M-RoPE. Skipping the encoder on media is where the largest speed-ups come from.' },
  { h: 'Certified, not hoped', b: 'Each edit is scored against a fresh re-ingest. Safe edits are served; the rest fall back to a clean re-prefill. Correctness is never traded for speed.' },
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
            Your agent's memory shouldn't be disposable.
          </h1>
          <p className="t-body-lg fade-up delay-1 in" style={{ maxWidth: 680, marginBottom: 0, color: 'var(--stone-2)' }}>
            Splice turns a model's KV cache from a write-once buffer into working memory you can edit. Delete a span, replace it, reorder it, exactly and provably, without rebuilding the context from scratch.
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
            So when an agent trims old tool output, a stale screenshot, or a paragraph that no longer matters, today's serving engines do the safe thing: evict everything from the edit point and prefill it again. The cost scales with everything after the edit, and it is paid again on the very next edit. For vision and audio models, re-ingesting also re-runs the encoder.
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

      {/* Proof strip */}
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
