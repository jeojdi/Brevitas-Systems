// SixTechniquesHub.jsx
// Hub-and-spoke node chart for the "One layer, six techniques" section.
// - Router node at center; 6 technique nodes radiating around it.
// - Loose horizontal scroll-snap on a strip below (mobile / secondary).
// - Hover a node → connection line animates (oscillating/wavy), node pops forward.
// - Arrow keys cycle the focused technique when this section is in view.
// - Selecting a technique reveals its detail panel under the hub.

const { useState: useSTH, useEffect: useEfSTH, useRef: useRfSTH, useMemo: useMmSTH } = React;

const TECHNIQUES = [
  {
    n: '01',
    title: 'Inter-agent message compression',
    short: 'Compress',
    body: "Each agent's output is compressed before being passed downstream. Redundant sentences removed, task-relevant structure preserved. Compression ratio tunable per pipeline.",
    tag: 'Per-hop',
    demo: 'how-it-works.html#compression',
  },
  {
    n: '02',
    title: 'Shared memory with content-addressed references',
    short: 'Reference',
    body: 'Agent outputs are stored once in session-scoped memory. Downstream agents receive stable references — IDs, not re-serialized content.',
    tag: 'Session-scoped',
  },
  {
    n: '03',
    title: 'Delta mode',
    short: 'Delta',
    body: 'After the first full context pass, subsequent agent calls receive only the changes to shared state. Analogous to how version control stopped sending full file copies.',
    tag: 'State-diff',
  },
  {
    n: '04',
    title: 'Smart context pruning',
    short: 'Prune',
    body: 'A relevance pass drops sections of inter-agent context unlikely to be used by the next agent, based on task class and agent role.',
    tag: 'Role-aware',
  },
  {
    n: '05',
    title: 'Compact message protocol',
    short: 'Protocol',
    body: 'A structured schema replaces free-form prose for inter-agent messages, reducing syntactic overhead by 20–40% in current tests.',
    tag: 'Schema',
  },
  {
    n: '06',
    title: 'Task-aware routing',
    short: 'Route',
    body: 'Determines which of the above to apply per call based on task class and pipeline shape. Not every call needs every optimization.',
    tag: 'Orchestrator',
  },
];

// Deterministic positions around the hub. We lay out on an ellipse (wider than tall)
// so the graph reads horizontally. Angles are picked to avoid label collisions.
// 0° = right; we go counter-clockwise visually (but in CSS y increases downward so
// we negate sin).
const LAYOUT = [
  { ang: -150 }, //  1 — upper-left
  { ang:  -90 }, //  2 — top
  { ang:  -30 }, //  3 — upper-right
  { ang:   30 }, //  4 — lower-right
  { ang:   90 }, //  5 — bottom
  { ang:  150 }, //  6 — lower-left
];

// Hub layout dimensions (inside the SVG + overlay). We use an SVG for connection
// lines and absolute-positioned divs for the node cards, sharing a coordinate system.
const HUB = {
  w: 1040,
  h: 620,
  cx: 520,
  cy: 310,
  rx: 340, // horizontal radius (leaves room for 220px-wide cards at each end)
  ry: 210, // vertical radius
};

function polarToXY(angDeg) {
  const a = (angDeg * Math.PI) / 180;
  return {
    x: HUB.cx + HUB.rx * Math.cos(a),
    y: HUB.cy + HUB.ry * Math.sin(a),
  };
}

// Build a wavy quadratic-bezier path from (x1,y1) → (x2,y2) with a given amplitude.
// We use TWO control points offset perpendicular to the line by +amp and -amp to get
// a shallow S-curve feel, but for performance we just use a single quadratic.
function wavyPath(x1, y1, x2, y2, amp = 0, phase = 0) {
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  // Perpendicular offset
  const dx = x2 - x1, dy = y2 - y1;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const offset = amp * Math.sin(phase);
  const cx = mx + nx * offset;
  const cy = my + ny * offset;
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

function HubNodeCard({ tech, pos, isActive, isDim, onEnter, onLeave, onClick }) {
  return (
    <button
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      onFocus={onEnter}
      onBlur={onLeave}
      onClick={onClick}
      data-sth-node-index={tech.idx}
      style={{
        position: 'absolute',
        left: (pos.x / HUB.w * 100) + '%',
        top: (pos.y / HUB.h * 100) + '%',
        transform: `translate(-50%, -50%) ${isActive ? 'scale(1.04)' : 'scale(1)'}`,
        width: 'min(220px, 26vw)',
        minWidth: 180,
        padding: '14px 16px',
        background: isActive ? 'var(--graphite)' : 'rgba(16,15,13,0.82)',
        border: '1px solid ' + (isActive ? 'var(--bronze)' : 'var(--line)'),
        boxShadow: isActive
          ? '0 0 0 3px rgba(138,98,66,0.18), 0 10px 30px rgba(0,0,0,0.4)'
          : '0 4px 14px rgba(0,0,0,0.25)',
        opacity: isDim ? 0.38 : 1,
        borderRadius: 4,
        textAlign: 'left',
        cursor: 'pointer',
        transition: 'transform 260ms cubic-bezier(.4,0,.2,1), border-color 260ms, box-shadow 260ms, opacity 260ms, background 260ms',
        zIndex: isActive ? 3 : 2,
        color: 'var(--bone)',
        fontFamily: 'inherit',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 6 }}>
        <span style={{
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 10,
          letterSpacing: '0.14em',
          color: isActive ? 'var(--bronze)' : 'var(--stone)',
          transition: 'color 260ms',
        }}>{tech.n}</span>
        <span style={{
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 9,
          letterSpacing: '0.1em',
          color: 'var(--stone-2)',
          textTransform: 'uppercase',
          marginLeft: 'auto',
        }}>{tech.tag}</span>
      </div>
      <div style={{
        fontFamily: 'Newsreader, serif',
        fontSize: 17,
        letterSpacing: '-0.01em',
        lineHeight: 1.2,
        color: 'var(--bone)',
      }}>{tech.short}</div>
      <div style={{
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
        color: 'var(--stone-2)',
        marginTop: 6,
        letterSpacing: '0.04em',
        lineHeight: 1.4,
        display: '-webkit-box',
        WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical',
        overflow: 'hidden',
      }}>{tech.title}</div>
    </button>
  );
}

function HubCenter({ isHovered }) {
  return (
    <div style={{
      position: 'absolute',
      left: (HUB.cx / HUB.w * 100) + '%',
      top: (HUB.cy / HUB.h * 100) + '%',
      transform: 'translate(-50%, -50%)',
      width: 'min(180px, 22vw)',
      minWidth: 150,
      padding: '18px 20px',
      background: 'var(--graphite)',
      border: '1px solid var(--bronze)',
      borderRadius: 4,
      boxShadow: '0 0 0 3px rgba(138,98,66,0.18), 0 12px 36px rgba(0,0,0,0.5)',
      textAlign: 'center',
      zIndex: 4,
      pointerEvents: 'none',
    }}>
      <div style={{
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
        letterSpacing: '0.18em',
        color: 'var(--bronze)',
        textTransform: 'uppercase',
        marginBottom: 8,
      }}>The Router</div>
      <div style={{
        fontFamily: 'Newsreader, serif',
        fontSize: 22,
        letterSpacing: '-0.015em',
        lineHeight: 1.15,
        color: 'var(--bone)',
        marginBottom: 8,
      }}><em style={{ fontStyle: 'italic' }}>Task-aware routing</em></div>
      <div style={{
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 9.5,
        letterSpacing: '0.08em',
        color: 'var(--stone-2)',
      }}>decides which to apply<br/>per call</div>
    </div>
  );
}

function SixTechniquesHub() {
  const [hoverIdx, setHoverIdx] = useSTH(null);     // idx user is hovering (transient)
  const [selectedIdx, setSelectedIdx] = useSTH(0);  // idx user has locked in (default: first)
  const [phase, setPhase] = useSTH(0);              // animation clock for wavy lines
  const [isVisible, setIsVisible] = useSTH(false);
  const rootRef = useRfSTH(null);
  const stripRef = useRfSTH(null);

  // Position map
  const positions = useMmSTH(() =>
    LAYOUT.map((lay, i) => ({ ...polarToXY(lay.ang), ang: lay.ang, idx: i })),
  []);

  // Oscillating wave clock — only ticks while something is hovered
  useEfSTH(() => {
    if (hoverIdx == null) return;
    let raf, start = performance.now();
    function tick(t) {
      setPhase(((t - start) / 520)); // ~0.52s period per radian stride
      raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [hoverIdx]);

  // Visibility gating for arrow keys
  useEfSTH(() => {
    if (!rootRef.current) return;
    const obs = new IntersectionObserver((entries) => {
      entries.forEach(e => setIsVisible(e.isIntersecting && e.intersectionRatio > 0.25));
    }, { threshold: [0, 0.25, 0.5] });
    obs.observe(rootRef.current);
    return () => obs.disconnect();
  }, []);

  // Arrow-key navigation
  useEfSTH(() => {
    if (!isVisible) return;
    function onKey(ev) {
      const t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      if (ev.key === 'ArrowRight') {
        ev.preventDefault();
        setSelectedIdx(i => (i + 1) % TECHNIQUES.length);
      } else if (ev.key === 'ArrowLeft') {
        ev.preventDefault();
        setSelectedIdx(i => (i - 1 + TECHNIQUES.length) % TECHNIQUES.length);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isVisible]);

  // Keep strip scrolled to the selected technique (loose snap)
  useEfSTH(() => {
    if (!stripRef.current) return;
    const child = stripRef.current.children[selectedIdx];
    if (child && child.scrollIntoView) {
      child.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    }
  }, [selectedIdx]);

  const activeIdx = hoverIdx != null ? hoverIdx : selectedIdx;
  const active = TECHNIQUES[activeIdx];

  return (
    <div ref={rootRef}>
      <style>{`
        @keyframes sthPulseDot {
          0% { transform: translate(-50%,-50%) scale(0.6); opacity: 0.4 }
          70% { transform: translate(-50%,-50%) scale(1.6); opacity: 0 }
          100% { transform: translate(-50%,-50%) scale(1.6); opacity: 0 }
        }
        .sth-hub-wrap { position: relative; width: 100%; }
        .sth-hub-stage {
          position: relative;
          width: 100%;
          aspect-ratio: ${HUB.w} / ${HUB.h};
          max-height: 640px;
        }
        .sth-hub-stage svg, .sth-hub-stage .sth-layer {
          position: absolute; inset: 0; width: 100%; height: 100%;
        }
        @media (max-width: 860px) {
          .sth-hub-stage { display: none; }
          .sth-hub-strip { display: flex !important; }
        }
        .sth-hub-strip {
          display: none;
        }
        .sth-hub-strip::-webkit-scrollbar { height: 4px }
        .sth-hub-strip::-webkit-scrollbar-thumb { background: var(--stone); border-radius: 2px }
      `}</style>

      {/* Stage: SVG lines + absolute-positioned nodes */}
      <div className="sth-hub-wrap">
        <div className="sth-hub-stage">
          <svg viewBox={`0 0 ${HUB.w} ${HUB.h}`} preserveAspectRatio="xMidYMid meet" style={{ overflow: 'visible' }}>
            <defs>
              <linearGradient id="sthLineActive" x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%"  stopColor="var(--bronze)" stopOpacity="0.1" />
                <stop offset="50%" stopColor="var(--bronze)" stopOpacity="0.95" />
                <stop offset="100%" stopColor="var(--signal)" stopOpacity="0.85" />
              </linearGradient>
              <radialGradient id="sthHubRing" cx="50%" cy="50%" r="50%">
                <stop offset="0%"  stopColor="var(--bronze)" stopOpacity="0.0" />
                <stop offset="80%" stopColor="var(--bronze)" stopOpacity="0.0" />
                <stop offset="100%" stopColor="var(--bronze)" stopOpacity="0.22" />
              </radialGradient>
            </defs>

            {/* Ambient orbit ring */}
            <ellipse
              cx={HUB.cx} cy={HUB.cy}
              rx={HUB.rx} ry={HUB.ry}
              fill="none"
              stroke="var(--line)"
              strokeDasharray="2 7"
              strokeWidth="1"
              opacity="0.55"
            />

            {/* Spokes: one line per technique */}
            {positions.map((p, i) => {
              const isActive = i === activeIdx;
              const isHoveredExactly = hoverIdx === i;
              // Amplitude and dash animation — only oscillate when that spoke is hovered
              const amp = isHoveredExactly ? 28 : 0;
              const d = wavyPath(HUB.cx, HUB.cy, p.x, p.y, amp, phase + i * 0.6);
              return (
                <g key={i}>
                  {/* Base dormant line */}
                  <path
                    d={`M ${HUB.cx} ${HUB.cy} L ${p.x} ${p.y}`}
                    stroke="var(--line)"
                    strokeWidth="1"
                    fill="none"
                    opacity={isActive ? 0 : 0.85}
                    style={{ transition: 'opacity 300ms' }}
                  />
                  {/* Active wavy line */}
                  <path
                    d={d}
                    stroke={isHoveredExactly ? 'url(#sthLineActive)' : 'var(--bronze)'}
                    strokeWidth={isHoveredExactly ? 2 : 1.4}
                    fill="none"
                    opacity={isActive ? 1 : 0}
                    strokeLinecap="round"
                    style={{ transition: 'opacity 260ms, stroke-width 260ms' }}
                  />
                  {/* Endpoint dot on the node side, pulses when active */}
                  <circle
                    cx={p.x} cy={p.y}
                    r={isActive ? 4 : 2.5}
                    fill={isActive ? 'var(--bronze)' : 'var(--stone)'}
                    opacity={isActive ? 0.9 : 0.6}
                    style={{ transition: 'r 260ms, opacity 260ms' }}
                  />
                </g>
              );
            })}

            {/* Center hub ring */}
            <circle cx={HUB.cx} cy={HUB.cy} r="72" fill="url(#sthHubRing)" />
          </svg>

          <div className="sth-layer" style={{ pointerEvents: 'none' }}>
            <HubCenter />
            <div style={{ pointerEvents: 'auto', position: 'absolute', inset: 0 }}>
              {TECHNIQUES.map((t, i) => (
                <HubNodeCard
                  key={i}
                  tech={{ ...t, idx: i }}
                  pos={positions[i]}
                  isActive={i === activeIdx}
                  isDim={hoverIdx != null && hoverIdx !== i}
                  onEnter={() => setHoverIdx(i)}
                  onLeave={() => setHoverIdx(null)}
                  onClick={() => setSelectedIdx(i)}
                />
              ))}
            </div>
          </div>
        </div>

        {/* Mobile / alt: horizontal scroll-snap strip */}
        <div
          ref={stripRef}
          className="sth-hub-strip"
          style={{
            gap: 14,
            overflowX: 'auto',
            scrollSnapType: 'x proximity',
            paddingBottom: 14,
            WebkitOverflowScrolling: 'touch',
          }}
        >
          {TECHNIQUES.map((t, i) => {
            const isActive = i === activeIdx;
            return (
              <button
                key={i}
                onClick={() => setSelectedIdx(i)}
                style={{
                  flex: '0 0 80%',
                  scrollSnapAlign: 'center',
                  padding: '18px 18px',
                  border: '1px solid ' + (isActive ? 'var(--bronze)' : 'var(--line)'),
                  background: isActive ? 'var(--graphite)' : 'transparent',
                  color: 'var(--bone)',
                  borderRadius: 4,
                  textAlign: 'left',
                  cursor: 'pointer',
                  minWidth: 240,
                }}
              >
                <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, letterSpacing: '0.14em', color: 'var(--bronze)', marginBottom: 6 }}>{t.n} · {t.tag}</div>
                <div style={{ fontFamily: 'Newsreader, serif', fontSize: 18, letterSpacing: '-0.01em', marginBottom: 6 }}>{t.short}</div>
                <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: 'var(--stone-2)', lineHeight: 1.45 }}>{t.title}</div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Detail panel for the active technique */}
      <div style={{
        marginTop: 22,
        border: '1px solid var(--line)',
        background: 'rgba(16,15,13,0.55)',
        padding: '26px 28px',
        borderRadius: 4,
        display: 'grid',
        gridTemplateColumns: '120px 1fr auto',
        gap: 28,
        alignItems: 'start',
      }}>
        <div>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 38,
            fontWeight: 300,
            color: 'var(--bronze)',
            letterSpacing: '-0.02em',
          }}>{active.n}</div>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 10,
            letterSpacing: '0.14em',
            color: 'var(--stone-2)',
            textTransform: 'uppercase',
            marginTop: 4,
          }}>{active.tag}</div>
        </div>
        <div>
          <div style={{
            fontFamily: 'Newsreader, serif',
            fontSize: 24,
            letterSpacing: '-0.015em',
            color: 'var(--bone)',
            marginBottom: 10,
            lineHeight: 1.2,
          }}>{active.title}</div>
          <p style={{
            fontFamily: 'Newsreader, serif',
            fontSize: 15,
            lineHeight: 1.65,
            color: 'var(--stone-2)',
            margin: 0,
            maxWidth: 680,
          }}>{active.body}</p>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-end' }}>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace', fontSize: 9.5, letterSpacing: '0.1em',
            color: isVisible ? 'var(--bronze)' : 'var(--stone)',
            textTransform: 'uppercase', display: 'flex', gap: 6, alignItems: 'center',
          }} title={isVisible ? 'Arrow keys active' : 'Scroll into view to use arrow keys'}>
            <kbd style={sthKbd}>←</kbd>
            <kbd style={sthKbd}>→</kbd>
            <span style={{ opacity: 0.75 }}>navigate</span>
          </div>
          {active.demo && (
            <a href={active.demo} style={{
              fontFamily: 'JetBrains Mono, monospace',
              fontSize: 10.5,
              letterSpacing: '0.08em',
              color: 'var(--bronze)',
              textDecoration: 'none',
              borderBottom: '1px solid var(--bronze)',
              paddingBottom: 2,
            }}>see it in action →</a>
          )}
        </div>
      </div>
    </div>
  );
}

const sthKbd = {
  display: 'inline-block',
  padding: '2px 6px',
  border: '1px solid currentColor',
  borderRadius: 2,
  fontFamily: 'JetBrains Mono, monospace',
  fontSize: 10,
  lineHeight: 1,
};

Object.assign(window, { SixTechniquesHub });
