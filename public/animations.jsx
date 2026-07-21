// Brevitas — three signature animations
// A: Compression Sequence · B: Pipeline Reduction · C: Thesis Sequence

const { useState: useStateA, useEffect: useEffectA, useRef: useRefA, useCallback: useCallbackA } = React;

// ============================================================
// A — Context Handoff (inter-agent compression)
// ============================================================
// Agent 1's output is handed to Agent 2. Context Agent 2 already has is pruned,
// verbose notes are compressed, and genuinely new information survives.

const HANDOFF_CHUNKS = [
  // { tag: 'new' | 'seen' | 'trim', text, short? }
  { tag: 'new',  text: 'Task — limit each person to 100 requests per minute, using Redis to track usage.' },
  { tag: 'seen', text: 'The app has three parts: the interface, business logic, and data storage.' },
  { tag: 'seen', text: 'All database activity goes through the data layer.' },
  { tag: 'new',  text: 'Apply the limit safely in Redis so requests arriving at the same time are counted correctly.' },
  { tag: 'trim', text: 'Check the limit before routing, rather than waiting until a blocked request reaches the app and does unnecessary work, so blocked requests stop immediately.', short: 'Check the limit before routing, so blocked requests stop immediately.' },
  { tag: 'seen', text: 'The project uses Python 3.13, Redis, and pytest.' },
];

const SIMPLIFY_WORDS = [
  ['Check', true], ['the', true], ['limit', true], ['before', true], ['routing,', true],
  ['rather', false], ['than', false], ['waiting', false], ['until', false], ['a', false],
  ['blocked', false], ['request', false], ['reaches', false], ['the', false], ['app', false],
  ['and', false], ['does', false], ['unnecessary', false], ['work,', false],
  ['so', true], ['blocked', true], ['requests', true], ['stop', true], ['immediately.', true],
];

function SimplifyingChunk({ phase, simplified }) {
  if (phase === 'done') return <span>{simplified}</span>;

  const dropping = phase === 'dropout';
  return (
    <span>
      {SIMPLIFY_WORDS.map(([word, kept], i) => (
        <span key={i} style={{
          display: 'inline-block',
          marginRight: 4,
          opacity: dropping && !kept ? 0.12 : 1,
          color: dropping ? (kept ? 'var(--signal)' : 'var(--stone)') : 'inherit',
          textDecoration: dropping && !kept ? 'line-through' : 'none',
          textDecorationColor: 'var(--stone)',
          textDecorationThickness: '1px',
          transition: `opacity 280ms var(--ease-out-soft) ${i * 18}ms, color 300ms var(--ease-out-soft) ${i * 18}ms`,
        }}>
          {word}
        </span>
      ))}
    </span>
  );
}

function CompressionSequence() {
  const [phase, setPhase] = useStateA('idle'); // idle, typing, hold, dropout, done
  const [visibleCount, setVisibleCount] = useStateA(0);
  const [tokens, setTokens] = useStateA(2400);
  const [key, setKey] = useStateA(0);
  const [ref, inView] = useInView({ threshold: 0.35 });
  const reduced = useReducedMotion();

  const total = HANDOFF_CHUNKS.length;

  useEffectA(() => {
    if (!inView) return;
    if (reduced) { setPhase('done'); setVisibleCount(total); setTokens(900); return; }
    let timers = [];
    setPhase('typing');
    setVisibleCount(0);
    setTokens(2400);
    // Reveal each context chunk in turn
    for (let i = 1; i <= total; i++) {
      timers.push(setTimeout(() => setVisibleCount(i), (i / total) * 1000 + 120));
    }
    timers.push(setTimeout(() => setPhase('hold'), 1500));
    timers.push(setTimeout(() => setPhase('dropout'), 2300));
    // Animate token counter down as redundant context is pruned + trimmed
    timers.push(setTimeout(() => {
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / 1400);
        const eased = p < 0.5 ? 2*p*p : 1 - Math.pow(-2*p+2,2)/2;
        setTokens(Math.round(2400 - 1500 * eased));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, 2300));
    timers.push(setTimeout(() => setPhase('done'), 3800));
    return () => timers.forEach(clearTimeout);
  }, [inView, key, reduced, total]);

  const replay = () => { setVisibleCount(0); setTokens(2400); setPhase('idle'); setKey(k => k+1); };

  const isAfter = phase === 'dropout' || phase === 'done';

  return (
    <div ref={ref} className="card" style={{ padding: 0, overflow: 'hidden', background: 'var(--ink-2)' }}>
      <div style={{
        padding: '18px 24px',
        borderBottom: '1px solid var(--line)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        gap: 16, flexWrap: 'wrap',
      }}>
        <div className="t-overline" style={{ color: 'var(--stone-2)' }}>
          {isAfter ? 'WHAT AGENT 2 RECEIVES' : 'AGENT 1 SHARES NOTES WITH AGENT 2'}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div className="t-mono tabular" style={{ color: isAfter ? 'var(--signal)' : 'var(--oxblood)' }}>
            {tokens.toLocaleString()} tokens to process
          </div>
          {isAfter && (
            <div className="t-mono" style={{
              color: 'var(--signal)',
              padding: '2px 8px',
              border: '1px solid var(--signal)',
              borderRadius: 2,
              fontSize: 11,
            }}>
              62% less
            </div>
          )}
        </div>
      </div>
      <div style={{
        padding: '24px 26px 20px',
        minHeight: 260,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
      }}>
        {HANDOFF_CHUNKS.map((c, i) => {
          const visible = i < visibleCount;
          const pruned = isAfter && c.tag === 'seen';
          const kept = isAfter && c.tag === 'new';
          const chipLabel = isAfter
            ? { new: 'KEPT', seen: 'REMOVED', trim: 'SIMPLIFIED' }[c.tag]
            : { new: 'NEW', seen: 'REPEATED', trim: 'WORDY' }[c.tag];
          const chipColor = c.tag === 'new'
            ? 'var(--signal)'
            : c.tag === 'trim' ? 'var(--bronze)' : 'var(--stone)';
          return (
            <div
              key={i}
              style={{
                display: 'flex',
                gap: 12,
                alignItems: 'flex-start',
                opacity: visible ? (pruned ? 0.3 : 1) : 0,
                transform: visible ? 'translateY(0)' : 'translateY(4px)',
                transition: `opacity 320ms var(--ease-out-soft) ${i * 30}ms, transform 320ms var(--ease-out-soft) ${i * 30}ms`,
              }}
            >
              <span className="t-mono" style={{
                flex: '0 0 68px',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                marginTop: 6,
                color: chipColor,
                fontSize: 8.5,
                letterSpacing: '0.08em',
                opacity: 0.66,
                whiteSpace: 'nowrap',
              }}>
                <span aria-hidden="true" style={{
                  width: 4,
                  height: 4,
                  borderRadius: '50%',
                  background: 'currentColor',
                  flexShrink: 0,
                }} />
                {chipLabel}
              </span>
              <span style={{
                flex: 1,
                fontSize: 13.5,
                lineHeight: 1.6,
                color: kept ? 'var(--signal)' : (pruned ? 'var(--stone)' : 'var(--bone-dim)'),
                transition: 'color 300ms var(--ease-out-soft)',
              }}>
                <span style={{
                  textDecoration: pruned ? 'line-through' : 'none',
                  textDecorationColor: 'var(--stone)',
                }}>
                  {c.tag === 'trim'
                    ? <SimplifyingChunk phase={phase} simplified={c.short} />
                    : c.text}
                </span>
                {pruned && (
                  <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11, marginLeft: 8, whiteSpace: 'nowrap' }}>
                    · already known
                  </span>
                )}
              </span>
            </div>
          );
        })}
      </div>
      <div style={{
        padding: '12px 24px',
        borderTop: '1px solid var(--line)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        gap: 12, flexWrap: 'wrap',
        background: 'rgba(0,0,0,0.15)',
      }}>
        <div className="t-mono" style={{ color: 'var(--stone)', fontSize: 12 }}>
          Repeated details removed automatically
        </div>
        <button onClick={replay} className="t-mono" style={{
          background: 'transparent', border: '1px solid var(--line)',
          color: 'var(--fg-dim)', padding: '4px 10px', borderRadius: 2,
          cursor: 'pointer', fontSize: 12, marginLeft: 'auto',
        }}>
          Watch again ↻
        </button>
      </div>
    </div>
  );
}

// ============================================================
// B — Pipeline Reduction  (THE hero animation)
// ============================================================

function PipelineReduction({ compact = false, loop = true }) {
  const [ref, inView] = useInView({ threshold: 0.25 });
  const reduced = useReducedMotion();
  const [stage, setStage] = useStateA(0);
  // stages: 0 idle, 1 A1 active, 2 after A1 arrows, 3 A2 active + arrows, 4 A3 active, 5 totals, 6 resolution
  const [baselineTokens, setBaselineTokens] = useStateA(0);
  const [optimizedTokens, setOptimizedTokens] = useStateA(0);
  const [resolveIn, setResolveIn] = useStateA(false);
  const [cycle, setCycle] = useStateA(0);

  useEffectA(() => {
    if (!inView) return;
    if (reduced) {
      setStage(5); setBaselineTokens(2924); setOptimizedTokens(1188);
      setResolveIn(true);
      return;
    }
    let timers = [];
    setStage(0); setBaselineTokens(0); setOptimizedTokens(0); setResolveIn(false);
    timers.push(setTimeout(() => setStage(1), 500));
    timers.push(setTimeout(() => {
      setStage(2);
      // tick tokens
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / 900);
        setBaselineTokens(Math.round(1000 * p));
        setOptimizedTokens(Math.round(300 * p));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, 900));
    timers.push(setTimeout(() => {
      setStage(3);
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / 900);
        setBaselineTokens(Math.round(1000 + 500 * p));
        setOptimizedTokens(Math.round(300 + 450 * p));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, 1800));
    timers.push(setTimeout(() => {
      setStage(4);
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / 900);
        setBaselineTokens(Math.round(1500 + 1424 * p));
        setOptimizedTokens(Math.round(750 + 438 * p));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, 2700));
    timers.push(setTimeout(() => setStage(5), 3600));
    timers.push(setTimeout(() => {
      setResolveIn(true);
      setResolveIn(true);
    }, 4400));
    if (loop) {
      timers.push(setTimeout(() => setCycle(c => c+1), 12000));
    }
    return () => timers.forEach(clearTimeout);
  }, [inView, cycle, reduced, loop]);

  const stageActive = (n) => stage >= n;

  const AgentBox = ({ label, sublabel, active, side }) => (
    <div style={{
      border: `1.5px solid ${active ? 'var(--fg)' : 'var(--line)'}`,
      background: active ? 'var(--ink-3)' : 'var(--ink-2)',
      padding: '14px 18px',
      minWidth: 160,
      textAlign: 'center',
      borderRadius: 2,
      transition: 'border-color 400ms var(--ease-out-standard), background 400ms var(--ease-out-standard)',
    }}>
      <div className="t-mono" style={{ color: 'var(--stone-2)', fontSize: 11 }}>{sublabel}</div>
      <div className="serif" style={{ fontSize: 18, fontWeight: 400, letterSpacing: '-0.01em', color: 'var(--fg)', marginTop: 4 }}>{label}</div>
    </div>
  );

  const BrevitasBox = ({ visible, active }) => (
    <div style={{
      border: '1.5px double var(--signal)',
      background: 'var(--ink-2)',
      padding: '10px 16px',
      minWidth: 180,
      textAlign: 'center',
      borderRadius: 2,
      opacity: visible ? 1 : 0,
      boxShadow: active ? '0 0 24px var(--signal-glow)' : 'none',
      transition: 'opacity 400ms, box-shadow 400ms',
    }}>
      <div className="t-mono" style={{ color: 'var(--signal)', fontSize: 10, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
        BREVITAS LAYER
      </div>
      <div className="t-mono" style={{ color: 'var(--stone-2)', fontSize: 10, marginTop: 2 }}>
        compress · reference · delta
      </div>
    </div>
  );

  // Arrow: side 'left' is oxblood/thick, 'right' is signal/thin
  const Arrow = ({ side, thick = false, visible, label }) => {
    const color = side === 'left' ? 'var(--oxblood)' : 'var(--signal)';
    const width = side === 'left' ? (thick ? 6 : 3) : 2;
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
        opacity: visible ? 1 : 0,
        transition: 'opacity 300ms, transform 600ms var(--ease-out-soft)',
      }}>
        <div style={{
          width,
          height: 36,
          background: color,
          opacity: side === 'left' ? 0.55 : 0.85,
          transition: 'width 600ms var(--ease-out-soft)',
        }}/>
        <div style={{ color, opacity: 0.85, fontSize: 10, lineHeight: 1 }}>▼</div>
        {label && (
          <div className="t-mono tabular" style={{ color: side === 'left' ? 'var(--oxblood)' : 'var(--signal)', fontSize: 11 }}>
            {label}
          </div>
        )}
      </div>
    );
  };

  return (
    <div ref={ref} style={{
      border: '1px solid var(--line)',
      background: 'var(--ink-2)',
      borderRadius: 4,
      padding: compact ? 24 : 'clamp(28px, 4vw, 56px)',
      position: 'relative',
      overflow: 'hidden',
    }}>
      {/* Main split */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1px 1fr',
        gap: 0,
        opacity: resolveIn ? 0.2 : 1,
        transition: 'opacity 800ms var(--ease-out-soft)',
      }}>
        {/* LEFT: baseline */}
        <div style={{ padding: '0 clamp(12px, 2vw, 32px)' }}>
          <div className="t-overline" style={{ color: 'var(--stone-2)', marginBottom: 6 }}>BASELINE PIPELINE</div>
          <div className="t-mono" style={{ color: 'var(--stone)', marginBottom: 28, fontSize: 12 }}>how every team builds this today</div>

          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0 }}>
            <AgentBox label="Architect" sublabel="AGENT 1 · DeepSeek" active={stage === 1 || (stage >= 1 && stage <= 2)} />
            <Arrow side="left" thick={stage >= 2} visible={stage >= 2} label={stage >= 2 ? '1,000 tok' : ''} />
            <AgentBox label="Builder" sublabel="AGENT 2 · Groq" active={stage === 3} />
            <Arrow side="left" thick={stage >= 3} visible={stage >= 3} label={stage >= 3 ? '1,500 tok' : ''} />
            <AgentBox label="Reviewer" sublabel="AGENT 3 · OpenAI" active={stage === 4} />
          </div>
          <div style={{ marginTop: 32, textAlign: 'center' }}>
            <div className="t-mono" style={{ color: 'var(--stone-2)', fontSize: 11 }}>CUMULATIVE</div>
            <div className="serif tabular" style={{ fontSize: 36, fontWeight: 300, color: 'var(--oxblood)', letterSpacing: '-0.02em', marginTop: 4 }}>
              ▶ {baselineTokens.toLocaleString()} <span style={{ fontSize: 16, color: 'var(--stone-2)' }}>tokens</span>
            </div>
          </div>
        </div>

        {/* Divider */}
        <div style={{ background: 'var(--line)', width: 1, height: '100%' }} />

        {/* RIGHT: with brevitas */}
        <div style={{ padding: '0 clamp(12px, 2vw, 32px)' }}>
          <div className="t-overline" style={{ color: 'var(--signal)', marginBottom: 6 }}>
            <span className="overline-dot" style={{ background: 'var(--signal)' }}/> WITH BREVITAS
          </div>
          <div className="t-mono" style={{ color: 'var(--stone)', marginBottom: 28, fontSize: 12 }}>the same task, optimized</div>

          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0 }}>
            <AgentBox label="Architect" sublabel="AGENT 1 · DeepSeek" active={stage === 1} />
            <Arrow side="right" visible={stage >= 2} label={stage >= 2 ? '300 tok · compressed' : ''} />
            <BrevitasBox visible={stage >= 2} active={stage === 2} />
            <Arrow side="right" visible={stage >= 2} />
            <AgentBox label="Builder" sublabel="AGENT 2 · Groq" active={stage === 3} />
            <Arrow side="right" visible={stage >= 3} label={stage >= 3 ? '450 tok · delta' : ''} />
            <BrevitasBox visible={stage >= 3} active={stage === 3} />
            <Arrow side="right" visible={stage >= 3} />
            <AgentBox label="Reviewer" sublabel="AGENT 3 · OpenAI" active={stage === 4} />
          </div>
          <div style={{ marginTop: 32, textAlign: 'center' }}>
            <div className="t-mono" style={{ color: 'var(--stone-2)', fontSize: 11 }}>CUMULATIVE</div>
            <div className="serif tabular" style={{ fontSize: 36, fontWeight: 300, color: 'var(--signal)', letterSpacing: '-0.02em', marginTop: 4 }}>
              ▶ {optimizedTokens.toLocaleString()} <span style={{ fontSize: 16, color: 'var(--stone-2)' }}>tokens</span>
            </div>
          </div>
        </div>
      </div>

      {/* Resolution frame */}
      {resolveIn && !compact && (
        <div style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          alignItems: 'center',
          background: 'rgba(15,20,16,0.86)',
          backdropFilter: 'blur(6px)',
          padding: 24,
          animation: 'fadeIn 700ms var(--ease-out-soft)',
        }}>
          <div style={{ display: 'flex', gap: 'clamp(24px, 5vw, 64px)', alignItems: 'stretch', flexWrap: 'wrap', justifyContent: 'center' }}>
            <div style={{ textAlign: 'center' }}>
              <div className="serif" style={{ fontSize: 'clamp(26px, 4vw, 42px)', fontWeight: 300, color: 'var(--signal)', letterSpacing: '-0.02em', lineHeight: 1.1 }}>
                Input reduced
              </div>
              <div className="t-mono" style={{ color: 'var(--stone-2)', marginTop: 10 }}>optional compression</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div className="serif" style={{ fontSize: 'clamp(26px, 4vw, 42px)', fontWeight: 300, color: 'var(--signal)', letterSpacing: '-0.02em', lineHeight: 1.1 }}>
                Cache discounted
              </div>
              <div className="t-mono" style={{ color: 'var(--stone-2)', marginTop: 10 }}>same provider input</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div className="serif" style={{ fontSize: 'clamp(26px, 4vw, 42px)', fontWeight: 300, color: 'var(--fg)', letterSpacing: '-0.02em', lineHeight: 1.1 }}>
                Calls avoided
              </div>
              <div className="t-mono" style={{ color: 'var(--stone-2)', marginTop: 10 }}>exact response replay</div>
            </div>
          </div>
          <div className="t-mono" style={{ color: 'var(--stone)', marginTop: 32, textAlign: 'center' }}>
            Illustrative flow · each mechanism is metered separately
          </div>
        </div>
      )}

      <style>{`
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
      `}</style>
    </div>
  );
}

// ============================================================
// C — Thesis Sequence
// ============================================================

function ThesisSequence() {
  const [ref, inView] = useInView({ threshold: 0.3 });
  const reduced = useReducedMotion();
  const [s, setS] = useStateA(0); // 0..5

  useEffectA(() => {
    if (!inView) return;
    if (reduced) { setS(5); return; }
    const ts = [
      setTimeout(() => setS(1), 300),
      setTimeout(() => setS(2), 900),
      setTimeout(() => setS(3), 1600),
      setTimeout(() => setS(4), 2400),
      setTimeout(() => setS(5), 2900),
    ];
    return () => ts.forEach(clearTimeout);
  }, [inView, reduced]);

  const Line = ({ visible, children, caption, captionVisible, emphasis = false }) => (
    <div style={{ marginBottom: 'clamp(40px, 6vh, 72px)' }}>
      <div
        className={emphasis ? 't-display' : 't-h2'}
        style={{
          opacity: visible ? 1 : 0,
          transform: visible ? 'translateY(0)' : 'translateY(16px)',
          transition: 'opacity 450ms var(--ease-out-soft), transform 450ms var(--ease-out-soft)',
          color: emphasis ? 'var(--fg)' : 'var(--bone-dim)',
          textWrap: 'balance',
        }}
      >
        {children}
      </div>
      {caption && (
        <div
          className="t-mono"
          style={{
            color: 'var(--stone)',
            marginTop: 14,
            opacity: captionVisible ? 1 : 0,
            transition: 'opacity 400ms 200ms var(--ease-out-soft)',
          }}
        >
          {caption}
        </div>
      )}
    </div>
  );

  return (
    <div ref={ref}>
      <div style={{
        opacity: s >= 1 ? 1 : 0,
        transition: 'opacity 400ms',
        marginBottom: 'clamp(48px, 7vh, 80px)',
        display: 'flex', flexDirection: 'column', alignItems: 'center',
      }}>
        <div className="t-overline">the state of the stack — 2026</div>
        <hr className="rule" style={{ width: 64, marginTop: 18, borderColor: 'var(--line)' }}/>
      </div>

      <Line visible={s >= 1} captionVisible={s >= 1} caption="// gpt-4.turbo · one model · one call">
        The 2023 default was a single prompt.
      </Line>

      <Line visible={s >= 2} captionVisible={s >= 2} caption="// orchestrators · 5 to 50 inter-agent calls per task">
        The 2026 default is a pipeline of agents.
      </Line>

      <div style={{ marginBottom: 'clamp(40px, 6vh, 72px)' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 20,
            opacity: s >= 3 ? 1 : 0,
            transition: 'opacity 400ms',
            marginBottom: 28,
          }}
        >
          <div style={{ flex: 1, height: 1, background: 'var(--line)' }} />
          <span className="t-mono" style={{ color: 'var(--stone)', fontSize: 11, letterSpacing: '0.10em' }}>
            and no one optimized
          </span>
          <div style={{ flex: 1, height: 1, background: 'var(--line)' }} />
        </div>
        <div
          className="t-h2"
          style={{
            opacity: s >= 4 ? 1 : 0,
            transform: s >= 4 ? 'translateY(0)' : 'translateY(16px)',
            transition: 'opacity 900ms var(--ease-out-soft), transform 900ms var(--ease-out-soft)',
            color: 'var(--fg)',
            textWrap: 'balance',
          }}
        >
          what flows <em style={{ fontStyle: 'italic', color: 'var(--bronze)' }}>between</em> them.
        </div>
        <div
          className="t-mono"
          style={{
            color: 'var(--stone)',
            marginTop: 16,
            opacity: s >= 4 ? 1 : 0,
            transition: 'opacity 400ms 400ms',
          }}
        >
          // until now
        </div>
      </div>

      <div style={{
        opacity: s >= 5 ? 1 : 0,
        transition: 'opacity 500ms',
        marginTop: 24,
      }}>
        <a href="/product" className="btn btn-ghost underline">
          See how →
        </a>
      </div>
    </div>
  );
}

Object.assign(window, {
  CompressionSequence, PipelineReduction, ThesisSequence,
});
