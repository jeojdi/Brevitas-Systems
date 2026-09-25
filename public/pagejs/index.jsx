const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const LOGOS = [
  { brand: 'yc', src: '/assets/ycombinator-logo.png', alt: 'Y Combinator', w: 480, h: 137 },
  { brand: 'deepmind', src: '/assets/deepmind-logo.svg', alt: 'Google DeepMind', w: 512, h: 119 },
  { brand: 'nvidia', src: '/assets/nvidia-logo.svg', darkSrc: '/assets/trusted-nvidia-dark-brevitas.svg', alt: 'NVIDIA', w: 164, h: 30 },
  { brand: 'phia', src: '/assets/phia-logo.png', alt: 'Phia', w: 400, h: 218 },
  { brand: 'anthropic', src: '/assets/trusted-anthropic-coral-brevitas.svg', alt: 'Anthropic', w: 1024, h: 115 },
];

const RESEARCH = [
  { url: '/blog/splice-certificate', title: 'Certifying a KV-cache edit: a teacher-forced KL gate against a fresh re-ingest.', dek: 'An edited cache is only worth serving if it matches a clean recompute. The gate, its zero-reading control, and the calibrated runtime threshold that decides what ships.', date: 'Sep 22, 2026', img: '/assets/ascii-magic-3-poster.jpg' },
  { url: '/blog/splice-repair', title: 'Dependency-ranked repair: recomputing only the rows a cache edit breaks.', dek: 'Residue after a mid-sequence delete is sparse. Rank the downstream rows by their dependency on the deleted span, recompute a scattered two to twenty percent, and skip the rest.', date: 'Sep 20, 2026', img: '/assets/ascii-magic-4-poster.jpg' },
  { url: '/blog/splice-rerotation', title: 'Exact position surgery: re-rotating a rotary KV cache after a mid-sequence edit.', dek: 'The correction after a delete is a single rotation per surviving key, and it is exact because rotations compose. The identity, its multi-axis M-RoPE form, and a zero-KL control.', date: 'Sep 18, 2026', img: '/assets/ascii-magic-5-poster.jpg' },
];

const FEATURES = [
  { h: 'Delete spans', b: 'Cut old tool output, stale screenshots or retrieved docs out of a live cache and close the gap. Every surviving token keeps the exact state the model computed for it.' },
  { h: 'Replace and reorder', b: 'Swap a span for one the same length, or move spans around the sequence. Rotary positions re-rotate exactly, so the edit is bit-for-bit what a fresh prefill would produce.' },
  { h: 'Every modality', b: 'The same edit works on text, video, images and audio, across 1-D RoPE and multi-axis M-RoPE. Skipping the encoder on media is where the largest speed-ups come from.' },
  { h: 'Certified, not hoped', b: 'Each edit is scored against a fresh re-ingest. Safe edits are served; the rest fall back to a clean re-prefill. Correctness is never traded for speed.' },
];

function LandingPage() {
  useFadeUpReveal();
  return (
    <>
      <Nav />
      <main id="main">

        {/* Hero */}
        <section className="section" style={{ paddingBottom: 0 }}>
          <div className="container">
            <h1 className="fade-up in" style={{ margin: '8px 0 24px', maxWidth: 900, fontFamily: "'Inter Tight', sans-serif", fontWeight: 450, fontSize: 'clamp(40px, 5.4vw, 68px)', letterSpacing: '-0.035em', lineHeight: 1.04, color: 'var(--fg)' }}>
              Your agent's memory shouldn't be disposable.
            </h1>
            <p className="t-body-lg fade-up delay-1 in" style={{ maxWidth: 680, marginBottom: 36, color: 'var(--stone-2)' }}>
              Splice turns a model's KV cache from a write-once buffer into working memory you can edit. Delete a span, replace it, reorder it, exactly and provably, without rebuilding the context from scratch.
            </p>
            <div className="fade-up delay-2 in" style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              <Button variant="primary" href={CALENDLY} target="_blank" className="hero-btn hero-cta">Book a call</Button>
              <Button variant="ghost" href="/benchmarks" className="hero-btn">See the benchmarks</Button>
            </div>
          </div>
        </section>

        {/* Trusted by */}
        <section className="section trust-strip" style={{ paddingTop: 'clamp(40px, 6vh, 64px)', paddingBottom: 0 }}>
          <div className="container">
            <p className="lbl">Trusted by teams from</p>
            <div className="trusted-logos">
              {LOGOS.map(({ brand, src, darkSrc, alt, w, h }) => (
                <div key={alt} className={`trusted-logo-item trusted-logo-item--${brand}`} role="img" aria-label={alt}>
                  {darkSrc && <img src={darkSrc} alt="" aria-hidden="true" width={w} height={h} className="trusted-logo trusted-logo--dark" />}
                  <img src={src} alt="" aria-hidden="true" width={w} height={h} className={`trusted-logo${darkSrc ? ' trusted-logo--light' : ''}`} />
                </div>
              ))}
            </div>
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

        {/* Research */}
        <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
          <div className="container">
            <div className="rb-sec-head">
              <h2 className="fade-up">Research</h2>
              <p className="fade-up">Our writing starts with a question: where does a model's memory actually break when you edit it, and how do you prove a repair is correct? These are the deep dives.</p>
            </div>
            <div className="rb-grid fade-up">
              {RESEARCH.map(p => (
                <a key={p.url} href={p.url} className="rb-card">
                  <div className="thumb"><img src={p.img} alt="" loading="lazy" /></div>
                  <h3>{p.title}</h3>
                  <p>{p.dek}</p>
                  <div className="meta">Research paper&nbsp;&nbsp;·&nbsp;&nbsp;{p.date}</div>
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

      </main>
      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<LandingPage />);
