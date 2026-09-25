const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const STAGES = [
  {
    id: 'assess', n: '01 · Assess', h: 'Benchmark your workload.',
    lead: 'Before anything is deployed, we measure where your pipeline spends its inference. Which edits your agents make, how much they re-prefill, and what an in-place edit would save on your own models and hardware.',
    props: [
      ['Profiled on your traffic.', 'Real sessions, not synthetic prompts, so the numbers reflect what you actually run.'],
      ['Per-edit and all-in.', 'Steady-state speed-up and the setup cost charged honestly, side by side.'],
      ['A re-runnable report.', 'Every result carries its certificate: pooled p95 KL against a fresh re-ingest.'],
    ],
    card: { kind: 'Benchmarks', h: 'Splice benchmarks', b: 'How we measure, and the results on text, video, image and audio.', href: '/benchmarks', read: 'View benchmarks', img: '/assets/ascii-magic-5-poster.jpg' },
  },
  {
    id: 'integrate', n: '02 · Integrate', h: 'Into SGLang or vLLM.',
    lead: 'Splice runs as a cache-edit path inside your serving engine, not as a separate service. Edits arrive over a socket, repair is batched by block, and the surrounding cache is reused untouched.',
    props: [
      ['Radix-cache backend.', 'Built into SGLang today, with vLLM support for direct KV-cache access.'],
      ['Per-model exactness tests.', 'Each model is verified before an edit type is enabled in production.'],
      ['Byte-identical repair.', 'Block-batched repair reproduces per-row repair exactly, at engine speed.'],
    ],
    card: { kind: 'Research paper', h: 'Exact position surgery', b: 'Re-rotating a rotary cache after an edit is exact, and how it extends to multi-axis M-RoPE.', href: '/blog/splice-rerotation', read: 'Read the paper', img: '/assets/ascii-magic-3-poster.jpg' },
  },
  {
    id: 'certify', n: '03 · Certify', h: 'Prove every edit.',
    lead: 'An edit is only served if it matches the answer a clean recompute would have produced. We calibrate that gate for your models and your edit types, so correctness is a property of the system, not a hope.',
    props: [
      ['Teacher-forced against re-ingest.', 'Pooled p95 KL under 0.05, with a control arm that must read zero.'],
      ['A conservative runtime threshold.', 'Edits that certify are served; the rest fall back to a clean re-prefill.'],
      ['Tuned per edit type.', 'Delete, same-length replace and reorder are each calibrated separately.'],
    ],
    card: { kind: 'Research paper', h: 'The correctness certificate', b: 'A teacher-forced KL gate calibrated into a runtime threshold that decides what ships.', href: '/blog/splice-certificate', read: 'Read the paper', img: '/assets/ascii-magic-4-poster.jpg' },
  },
  {
    id: 'operate', n: '04 · Operate', h: 'Run it in production.',
    lead: 'A forward-deployed engineer works alongside your team to put the edit path into your production loop, watch it under real load, and widen coverage as your workloads and models change.',
    props: [
      ['Forward-deployed.', 'We integrate against your stack and stay through rollout, not just a handoff.'],
      ['Admission-aware.', 'Scheduling that keeps the edit path safe under shared KV pressure.'],
      ['Coverage that grows.', 'As the runtime proxy improves, more safe edits move onto the fast path.'],
    ],
    card: { kind: 'Research paper', h: 'Dependency-ranked repair', b: 'Which rows an edit actually breaks, and why recomputing a scattered few percent is enough.', href: '/blog/splice-repair', read: 'Read the paper', img: '/assets/product-problem.jpg' },
  },
];

function EnterprisePage() {
  useFadeUpReveal();
  return (
    <>
      <Nav current="enterprise" />

      {/* Hero */}
      <section className="section" style={{ paddingBottom: 'clamp(24px, 4vh, 48px)' }}>
        <div className="container">
          <h1 className="ent-h1 fade-up in">Splice, deployed into your inference stack.</h1>
          <p className="ent-sub fade-up delay-1 in">
            We partner with teams running self-hosted models across the whole arc of a deployment: measuring where your pipeline rebuilds memory it could edit, integrating the cache-edit path into your serving engine, certifying every edit against a fresh re-ingest, and running it in production alongside your team.
          </p>
          <div className="fade-up delay-2 in">
            <Button variant="primary" href={CALENDLY} target="_blank" className="hero-btn hero-cta">Book a call</Button>
          </div>
        </div>
      </section>

      {/* Stages */}
      {STAGES.map((s, i) => (
        <section key={s.id} id={s.id} className="section" style={{ borderTop: '1px solid var(--line)', background: i % 2 ? 'var(--ink-2)' : 'transparent' }}>
          <div className="container">
            <div className={`ent-sec${i % 2 ? ' ent-sec--rev' : ''}`}>
              <div>
                <div className="ent-num fade-up">{s.n}</div>
                <h2 className="fade-up">{s.h}</h2>
                <p className="lead fade-up">{s.lead}</p>
                <ul className="ent-props fade-up">
                  {s.props.map(([b, t], i) => <li key={i}><b>{b}</b> {t}</li>)}
                </ul>
              </div>
              <a href={s.card.href} className="ent-card fade-up">
                <img src={s.card.img} alt="" loading="lazy" />
                <div className="kind">{s.card.kind}</div>
                <h3>{s.card.h}</h3>
                <p>{s.card.b}</p>
                <span className="read">{s.card.read}</span>
              </a>
            </div>
          </div>
        </section>
      ))}

      {/* Requirements */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container" style={{ maxWidth: 820 }}>
          <div className="ent-num fade-up">What we need from you</div>
          <h2 className="t-h2 fade-up" style={{ margin: '0 0 28px' }}>Requirements for setup.</h2>
          <dl className="ent-reqs fade-up">
            <dt>Model access</dt><dd>Self-hosted open models on SGLang or vLLM, or a platform hosting them, so the edit path has direct KV-cache access.</dd>
            <dt>Replicas</dt><dd>Dedicated or lightly shared replicas for the edit path.</dd>
            <dt>Position schedule</dt><dd>Standard RoPE or M-RoPE with a fixed schedule.</dd>
            <dt>Cache precision</dt><dd>fp16, bf16 or int8 keys and values.</dd>
            <dt>Per-model test</dt><dd>A per-model exactness test before enabling an edit type in production.</dd>
          </dl>
        </div>
      </section>

      {/* CTA */}
      <section className="section" style={{ background: 'var(--ink-2)', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 820 }}>
            <h2 className="t-h1" style={{ marginTop: 0, marginBottom: 20 }}>Collaborate with us.</h2>
            <p className="t-body-lg" style={{ marginBottom: 40, maxWidth: 680 }}>
              Tell us what you run and where it hurts. We will benchmark Splice on your workload and share the certificate numbers, then scope the integration from there.
            </p>
            <Button variant="primary" href={CALENDLY} target="_blank" className="hero-btn hero-cta">Book a call</Button>
          </div>
        </div>
      </section>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<EnterprisePage />);
