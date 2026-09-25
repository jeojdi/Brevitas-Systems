
function NotFoundPage() {
  const [tokens, setTokens] = useState([]);

  useEffect(() => {
    // Generate a "compressed" glitch animation of tokens dropping out
    const words = ['context', 'prompt', 'agent', 'message', 'token', 'memory', 'state', 'route', 'call', 'trace'];
    setTokens(Array.from({length: 28}).map((_, i) => ({
      id: i,
      w: words[i % words.length],
      left: Math.random() * 90 + 5,
      top: Math.random() * 90 + 5,
      delay: Math.random() * 1.5,
      dropped: Math.random() < 0.75,
    })));
  }, []);

  return (
    <>
      <Nav current="" />

      <section style={{
        minHeight: 'calc(100vh - 80px)',
        display: 'flex',
        alignItems: 'center',
        padding: 'clamp(40px, 8vh, 120px) 0',
        position: 'relative',
        overflow: 'hidden',
      }}>
        {/* Drifting tokens bg */}
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
          {tokens.map(t => (
            <span
              key={t.id}
              className={`nf-token ${t.dropped ? 'dropped' : ''}`}
              style={{
                position: 'absolute',
                left: `${t.left}%`,
                top: `${t.top}%`,
                animationDelay: `${t.delay}s`,
              }}
            >{t.w}</span>
          ))}
        </div>

        <div className="container" style={{ position: 'relative', zIndex: 1 }}>
          <div style={{ maxWidth: 720 }}>
            <div className="t-mono" style={{ color: 'var(--bronze)', marginBottom: 24, fontSize: 13 }}>
              STATUS 404 · ROUTE NOT FOUND
            </div>
            <h1 className="serif" style={{
              fontSize: 'clamp(96px, 18vw, 220px)',
              fontWeight: 300,
              letterSpacing: '-0.04em',
              lineHeight: 0.9,
              margin: '0 0 24px 0',
            }}>
              4<em style={{ fontStyle: 'italic', color: 'var(--bronze)' }}>0</em>4
            </h1>
            <h2 className="serif" style={{
              fontSize: 'clamp(28px, 3.6vw, 42px)',
              fontWeight: 400,
              letterSpacing: '-0.015em',
              lineHeight: 1.15,
              margin: '0 0 24px 0',
              maxWidth: 640,
            }}>
              <em style={{ fontStyle: 'italic' }}>This page was compressed a little too aggressively.</em>
            </h2>
            <p className="t-body-lg" style={{ marginBottom: 48, maxWidth: 520 }}>
              The relevance model decided this route wasn't meaning-bearing. That was probably a mistake. Here are some routes that definitely are:
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 48 }}>
              <a href="/" className="btn btn-primary">Back to home</a>
              <a href="/product" className="btn btn-ghost underline">Product</a>
              <a href="/benchmarks" className="btn btn-ghost underline">Benchmarks</a>
              <a href="mailto:james@brevitassystems.com" className="btn btn-ghost underline">Docs</a>
            </div>

            <div style={{
              padding: '20px 24px',
              border: '1px solid var(--line)',
              background: 'var(--ink-2)',
              borderLeft: '2px solid var(--bronze)',
              maxWidth: 560,
              fontFamily: 'JetBrains Mono, monospace',
              fontSize: 12,
              color: 'var(--stone-2)',
            }}>
              <div style={{ color: 'var(--stone)', marginBottom: 6 }}># trace</div>
              <div>route = "{typeof window !== 'undefined' ? window.location.pathname : '/unknown'}"</div>
              <div style={{ color: 'var(--bronze)' }}>router.decide(route) → dropped (relevance: 0.03)</div>
              <div>→ <span style={{ color: 'var(--signal)' }}>redirect recommended: /</span></div>
            </div>
          </div>
        </div>
      </section>

      <Footer />

      <style>{`
        .nf-token {
          font-family: 'JetBrains Mono', monospace;
          font-size: 13px;
          color: var(--stone);
          opacity: 0.22;
          animation: nfDrift 8s ease-in-out infinite;
        }
        .nf-token.dropped {
          opacity: 0;
          text-decoration: line-through;
          animation: nfDrop 6s ease-in-out infinite;
        }
        @keyframes nfDrift {
          0%, 100% { transform: translate(0, 0); }
          50% { transform: translate(8px, -4px); }
        }
        @keyframes nfDrop {
          0% { opacity: 0.28; transform: translateY(0); text-decoration: none; }
          40% { opacity: 0.18; text-decoration: line-through; }
          70% { opacity: 0; transform: translateY(12px); }
          100% { opacity: 0; }
        }
      `}</style>
    </>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<NotFoundPage />);
