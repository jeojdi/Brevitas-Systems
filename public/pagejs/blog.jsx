const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const POSTS = [
  { slug: 'splice-certificate', url: '/blog/splice-certificate', title: 'Certifying a KV-cache edit: a teacher-forced KL gate against a fresh re-ingest.', dek: 'An edited cache is only worth serving if it matches a clean recompute. The gate, its zero-reading control, and the calibrated runtime threshold that decides what ships.', date: 'Sep 22, 2026', read: '10 min', tag: 'Research' },
  { slug: 'splice-repair', url: '/blog/splice-repair', title: 'Dependency-ranked repair: recomputing only the rows a cache edit breaks.', dek: 'Residue after a mid-sequence delete is sparse. Rank the downstream rows by their dependency on the deleted span, recompute a scattered two to twenty percent, and skip the rest.', date: 'Sep 20, 2026', read: '11 min', tag: 'Research' },
  { slug: 'splice-rerotation', url: '/blog/splice-rerotation', title: 'Exact position surgery: re-rotating a rotary KV cache after a mid-sequence edit.', dek: 'The correction after a delete is a single rotation per surviving key, and it is exact because rotations compose. The identity, its multi-axis M-RoPE form, and a zero-KL control.', date: 'Sep 18, 2026', read: '9 min', tag: 'Research' },
  { slug: 'splice-kv-cache-editing', url: '/blog/splice-kv-cache-editing', title: 'Splice: editing a live KV cache instead of re-prefilling it.', dek: 'Delete, replace and reorder spans inside a cached sequence, checked against a fresh re-ingest on every edit. Benchmarks on text, video and audio.', date: 'Sep 15, 2026', read: '7 min', tag: 'Research' },
  { slug: 'fable-5', url: '/blog/fable-5', title: 'Fable 5 is built for agents. The token bill will show it.', dek: 'Anthropic\'s Mythos-class model is finally public. The pricing changes the math on every multi-agent pipeline.', date: 'Jun 10, 2026', read: '8 min', tag: 'Engineering' },
  { slug: '60-percent-waste', url: '/blog/60-percent-waste', title: 'The 60% waste nobody talks about.', dek: 'Why multi-agent pipelines burn most of their tokens on redundant context, and what that costs at scale.', date: 'Apr 18, 2026', read: '9 min', tag: 'Research' },
  { slug: 'why-caches-dont-help', url: '/blog/why-caches-dont-help', title: 'Why prompt caches don\'t help multi-agent pipelines (much).', dek: 'Native prompt caching is great for single-call latency. It doesn\'t touch the biggest problem.', date: 'Apr 09, 2026', read: '6 min', tag: 'Engineering' },
  { slug: 'compression-without-losing-quality', url: '/blog/compression-without-losing-quality', title: 'Compressing agent messages without losing quality.', dek: 'A look at the relevance model, what it preserves, and how we measure parity against a 60-run baseline.', date: 'Mar 28, 2026', read: '11 min', tag: 'Method' },
  { slug: 'git-for-agent-context', url: '/blog/git-for-agent-context', title: 'Git solved this in 2005. Why haven\'t agents?', dek: 'Version control stopped sending full file copies two decades ago. Agent pipelines still send the entire conversation at every hop.', date: 'Mar 14, 2026', read: '5 min', tag: 'Essay' },
  { slug: 'harness', url: '/blog/harness', title: 'How we built the coding harness.', dek: 'Three-agent architect/builder/reviewer loop on 60 HumanEval+ tasks. Here\'s what we learned.', date: 'Mar 02, 2026', read: '8 min', tag: 'Benchmarks' },
  { slug: 'design-partner-program', url: '/blog/design-partner-program', title: 'Design partners wanted.', dek: 'We\'re taking on five more design partners this quarter. Here\'s what you get, and what we ask for.', date: 'Feb 22, 2026', read: '3 min', tag: 'Program' },
];

// One distinct thumbnail per post, keyed by slug (see /assets/blog/<slug>.jpg).
// Each is a unique cropped region of a brand poster, so no two posts share an image.
POSTS.forEach((p) => { if (!p.img) p.img = '/assets/blog/' + p.slug + '.jpg'; });

const FEATURED = [
  { title: 'Splice benchmarks', desc: 'Editing a live KV cache, measured against a full re-ingest.', href: '/benchmarks', cta: 'View benchmarks' },
  { title: 'How Splice works', desc: 'KV-cache surgery: delete, replace and reorder context in place, certified correct.', href: '/product', cta: 'See the product' },
];

function BlogPage() {
  useFadeUpReveal();
  const grid = POSTS.slice(0, 3);
  const rows = POSTS.slice(3);
  return (
    <>
      <Nav current="blog" />

      {/* Hero */}
      <section className="section" style={{ paddingBottom: 0 }}>
        <div className="container">
          <div className="rb-hero fade-up in">
            <h1 className="rb-title">Correctness makes all the difference.</h1>
            <p className="rb-sub">
              We're driven by the conviction that agents are turning the KV cache from a write-once buffer into working memory that changes every few turns. We build the systems that edit that memory in place, and prove every edit is correct before it is served.
            </p>
          </div>

          {/* Featured cards */}
          <div className="rb-featured fade-up">
            {FEATURED.map(f => (
              <a key={f.title} href={f.href} className="rb-fcard">
                <h3>{f.title}</h3>
                <div className="row">
                  <p>{f.desc}</p>
                  <span className="cta">{f.cta}</span>
                </div>
              </a>
            ))}
          </div>
        </div>
      </section>

      {/* Research grid */}
      <section className="section">
        <div className="container">
          <div className="rb-sec-head">
            <div>
              <h2>Research</h2>
              <p>Our writing starts with a question: where does a model's memory actually break when you edit it, and how do you prove a repair is correct? These are the deep dives.</p>
            </div>
          </div>
          <div className="rb-grid fade-up">
            {grid.map(p => (
              <a key={p.slug} href={p.url} className="rb-card">
                <div className="thumb"><img src={p.img} alt="" loading="lazy" /></div>
                <h3>{p.title}</h3>
                <p>{p.dek}</p>
                <div className="meta">{p.tag === 'Research' ? 'Research paper' : 'Blog'}&nbsp;&nbsp;·&nbsp;&nbsp;{p.date}</div>
              </a>
            ))}
          </div>
        </div>
      </section>

      {/* All writing, as rows */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <div className="rb-sec-head">
            <div><h2>More writing</h2></div>
          </div>
          <div className="rb-rows fade-up">
            {rows.map(p => (
              <a key={p.slug} href={p.url} className="rb-row">
                <div className="thumb" style={{ gridArea: 'thumb' }}><img src={p.img} alt="" loading="lazy" /></div>
                <div style={{ gridArea: 'title' }}>
                  <h3>{p.title}</h3>
                  <div className="meta">{p.date}&nbsp;&nbsp;·&nbsp;&nbsp;{p.read} read</div>
                </div>
                <span className="read" style={{ gridArea: 'read' }}>Read blog</span>
              </a>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="section" style={{ background: 'var(--ink-2)', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 820 }}>
            <h2 className="t-h2" style={{ marginTop: 0, marginBottom: 16 }}>Want the numbers on your own workload?</h2>
            <p className="t-body-lg" style={{ marginBottom: 32, maxWidth: 680 }}>
              We'll benchmark Splice on your models and contexts, and share the certificate numbers.
            </p>
            <Button variant="primary" href={CALENDLY} target="_blank" className="hero-btn hero-cta">Book a call</Button>
          </div>
        </div>
      </section>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<BlogPage />);
