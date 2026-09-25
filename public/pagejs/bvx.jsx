const CALENDLY = 'https://calendly.com/anish-brevitassystems/30min';

const FEATURES = [
  { h: 'Detects your tools', b: 'Finds your installed AI coding assistants and points each one at the local proxy. Every config change is backed up first, and nothing you have not approved is touched.' },
  { h: 'Provider caching, made to hit', b: 'Places cache breakpoints and keeps the request prefix byte-stable across steps, so the context your agent resends lands in the provider’s cheaper cached-input bucket instead of full price.' },
  { h: 'Billed on real savings', b: 'Cost is computed from the provider’s actual response and pricing. If the provider cache would have hit anyway, that discount is excluded, so you are only credited for savings bvx caused.' },
  { h: 'Nothing to change', b: 'No edits to your agents, prompts or provider. bvx runs as a background service you can start, stop and inspect with a single command.' },
];

function BvxPage() {
  useFadeUpReveal();
  return (
    <>
      <Nav current="product" />

      {/* Hero */}
      <section className="section" style={{ paddingBottom: 0 }}>
        <div className="container">
          <h1 className="fade-up in" style={{ margin: '8px 0 24px', maxWidth: 900, fontFamily: "'Inter Tight', sans-serif", fontWeight: 450, fontSize: 'clamp(40px, 5.4vw, 68px)', letterSpacing: '-0.035em', lineHeight: 1.04, color: 'var(--fg)' }}>
            Cut your coding agent's token bill with one command.
          </h1>
          <p className="t-body-lg fade-up delay-1 in" style={{ maxWidth: 700, marginBottom: 32, color: 'var(--stone-2)' }}>
            bvx is a CLI that routes your AI coding tools through a local proxy. It leans on the provider's own caching so the context your agents resend on every request is billed from a cheaper cache bucket instead of full price. No changes to your tools, prompts or provider.
          </p>
          <div className="fade-up delay-2 in" style={{ maxWidth: 720 }}>
            <InstallCommand />
          </div>
          <p className="t-mono fade-up delay-3 in" style={{ color: 'var(--stone)', fontSize: 12, marginTop: 14 }}>
            macOS · Linux · Windows. One line installs bvx, logs you in, and points your tools at the proxy.
          </p>
        </div>
      </section>

      {/* Problem */}
      <section className="section">
        <div className="container">
          <p className="prod-label fade-up">Problem</p>
          <h2 className="prod-head fade-up">Agents pay full price for context the model already saw.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            A coding agent resends most of its context on every step: the system prompt, the open files, the prior turns, the last tool output. The provider bills those tokens at the fresh input rate each time, even though it read them a moment ago.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 0 }}>
            Providers offer prompt caching that can serve repeated context from a far cheaper bucket, but only when the request is shaped to hit it. Most tools do not shape their requests that way, so the discount is left on the table on every single call.
          </p>
        </div>
      </section>

      {/* Solution */}
      <section className="section" style={{ borderTop: '1px solid var(--line)' }}>
        <div className="container">
          <p className="prod-label fade-up">Our solution</p>
          <h2 className="prod-head fade-up">A local proxy that makes provider caching actually hit.</h2>
          <p className="t-body fade-up" style={{ marginBottom: 20 }}>
            bvx installs a background proxy on <span className="mono">127.0.0.1:8080</span> and points each supported tool at it. Every request flows through the proxy, which places cache breakpoints and keeps the prefix stable so repeated context is served from the provider's cached-input bucket. Your tools, prompts and provider stay exactly as they are.
          </p>
          <p className="t-body fade-up" style={{ marginBottom: 32 }}>
            Savings are measured from what the provider actually billed, not what was requested. If bvx changes nothing, the baseline equals the real cost, so there is nothing to bill.
          </p>

          <div className="bvx-flow fade-up" role="img" aria-label="Request path: AI tool to local proxy to Brevitas to LLM provider and back.">
            <span className="mono" style={{ color: 'var(--stone)' }}># request path</span>{'\n'}
            AI tool  →  bvx proxy (127.0.0.1:8080)  →  brevitas  →  LLM provider  →  response
          </div>

          <p className="prod-label fade-up" style={{ marginTop: 40, marginBottom: 24 }}>What bvx does</p>
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

      {/* Install / CTA */}
      <section className="section" style={{ background: 'var(--ink-2)', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
        <div className="container">
          <div style={{ maxWidth: 820 }}>
            <h2 className="t-h1" style={{ marginTop: 0, marginBottom: 16 }}>Install it in one line.</h2>
            <p className="t-body-lg" style={{ marginBottom: 32, maxWidth: 680 }}>
              Add bvx to your machine, point your coding tools at it, and start saving on the tokens your agents were already resending.
            </p>
            <div style={{ maxWidth: 720, marginBottom: 28 }}>
              <InstallCommand />
            </div>
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              <Button variant="ghost" href="/docs" className="hero-btn">Read the docs</Button>
              <Button variant="ghost" href={CALENDLY} target="_blank" className="hero-btn" arrow={false}>Book a call</Button>
            </div>
          </div>
        </div>
      </section>

      <Footer />
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<BvxPage />);
