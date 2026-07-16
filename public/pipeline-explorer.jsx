// Brevitas — Pipeline Explorer (interactive, data-driven)
// Phases per hop:
//   Raw mode:      pending → typing → done
//   Brevitas mode: pending → typing → highlighting → deleting → done
// Depends on window.BrevitasPipelineData.

const { useState: usePE, useEffect: useEfPE, useRef: useRfPE, useMemo: useMmPE } = React;
const { TASKS, dropReason } = window.BrevitasPipelineData;

// ------------------------------------------------------------
// PToken — one word with phase-aware styling.
//   phase: 'typing' | 'highlighting' | 'deleting' | 'done'
//   mode:  'baseline' | 'optimized'
// ------------------------------------------------------------
// charEaten: -1 = full word visible, 0..len = number of chars eaten from the right
function PToken({ tok, mode, phase, onHover, charEaten, showRemoved = false }) {
  if (tok.k === 'space') return <span> </span>;
  const isKept = tok.k === 'kept';
  const isStruct = tok.k === 'structural';
  const isDroppable = tok.k === 'filler' || tok.k === 'redundant';

  // Determine visual state
  let highlighted = false; // red tint (marking for deletion)
  let beingEaten = false;

  if (mode === 'optimized' && isDroppable) {
    if (phase === 'highlighting') highlighted = true;
    else if (phase === 'deleting' || phase === 'done') { highlighted = true; beingEaten = true; }
  }

  // Character-level erase
  let visibleText = tok.t;
  let fullyGone = false;
  if (beingEaten && charEaten != null && charEaten >= 0) {
    const keep = Math.max(0, tok.t.length - charEaten);
    visibleText = tok.t.slice(0, keep);
    fullyGone = keep === 0;
  }

  const color = highlighted
    ? 'var(--oxblood)'
    : isKept ? 'var(--bone)' : isStruct ? 'var(--signal)' : 'var(--stone-2)';

  // When fully eaten, render an invisible placeholder to preserve the "gap" — user asked for holes left behind
  if (fullyGone) {
    if (showRemoved) {
      return <span className="bv-token-removed">{tok.t}</span>;
    }
    return (
      <span style={{
        fontFamily: isStruct ? 'JetBrains Mono, monospace' : 'inherit',
        fontSize: isStruct ? '0.92em' : 'inherit',
        color: 'transparent',
        userSelect: 'none',
      }}>{tok.t.replace(/./g, '\u00A0')}</span>
    );
  }

  return (
    <span
      onMouseEnter={() => onHover && onHover({ tok })}
      onMouseLeave={() => onHover && onHover(null)}
      style={{
        color,
        fontFamily: isStruct ? 'JetBrains Mono, monospace' : 'inherit',
        fontSize: isStruct ? '0.92em' : 'inherit',
        background: highlighted ? 'rgba(143,58,48,0.22)' : 'transparent',
        padding: 0,
        borderRadius: highlighted ? 2 : 0,
        transition: 'color 250ms, background 250ms',
        cursor: isDroppable || isKept ? 'help' : 'default',
        position: 'relative',
      }}
    >
      {visibleText}
      {beingEaten && !fullyGone && charEaten != null && charEaten > 0 && (
        <span style={{
          display: 'inline-block',
          width: 6,
          height: 14,
          background: 'var(--oxblood)',
          verticalAlign: 'text-bottom',
          marginLeft: 1,
          animation: 'bvCursorBlink 220ms steps(2) infinite',
        }} />
      )}
    </span>
  );
}

// ------------------------------------------------------------
// TypingBlock — tokens up to `reveal`, with inter-token spaces,
// plus a blinking cursor at the edge while still typing.
// ------------------------------------------------------------
function TypingBlock({ tokens, reveal, mode, phase, onHover, eatenMap, showRemoved = false }) {
  const shown = tokens.slice(0, reveal);
  const typing = phase === 'typing' && reveal < tokens.length;
  return (
    <div className="bv-transcript" style={{
      fontFamily: 'Newsreader, serif',
      fontSize: 15.5,
      lineHeight: 1.68,
      color: 'var(--bone)',
      display: 'grid',
    }}>
      {/* The hidden full transcript reserves the final layout before typing
          starts, so the animation never pushes the page downward. */}
      <div aria-hidden="true" style={{ gridArea: '1 / 1', visibility: 'hidden', pointerEvents: 'none' }}>
        {tokens.map((tok, i) => (
          <React.Fragment key={i}>
            <PToken tok={tok} mode="baseline" phase="done" charEaten={-1} />
            {i < tokens.length - 1 && tok.k !== 'space' && tokens[i + 1]?.k !== 'space' ? ' ' : ''}
          </React.Fragment>
        ))}
      </div>
      <div style={{ gridArea: '1 / 1', alignSelf: 'start' }}>
        {shown.map((tok, i) => (
          <React.Fragment key={i}>
            <PToken tok={tok} mode={mode} phase={phase} onHover={onHover} charEaten={eatenMap ? eatenMap[i] : -1} showRemoved={showRemoved} />
            {i < shown.length - 1 && tok.k !== 'space' && shown[i + 1]?.k !== 'space' ? ' ' : ''}
          </React.Fragment>
        ))}
        {typing && (
          <span style={{
            display: 'inline-block',
            width: 7,
            height: 15,
            background: 'var(--bronze)',
            verticalAlign: 'text-bottom',
            marginLeft: 2,
            animation: 'bvCursorBlink 800ms steps(2) infinite',
          }} />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------
// HopCard — one agent hop. Uses hop phase for styling.
// ------------------------------------------------------------
function HopCard({ role, subtitle, tokens, reveal, mode, phase, onHover, inputCost, outputCost, eatenMap }) {
  const active = phase === 'typing' || phase === 'highlighting' || phase === 'deleting';
  const pending = phase === 'pending';

  return (
    <div style={{
      flex: '1 1 0',
      minWidth: 0,
      borderTop: active ? '2px solid var(--bronze)' : '2px solid transparent',
      background: active ? 'var(--graphite)' : 'transparent',
      padding: '18px 22px 22px',
      transition: 'border-color 300ms, background 300ms, opacity 300ms',
      opacity: pending ? 0.4 : 1,
      position: 'relative',
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10.5, color: 'var(--bronze)', letterSpacing: '0.12em', marginBottom: 4 }}>
            {role}
          </div>
          <div style={{ fontFamily: 'Newsreader, serif', fontSize: 21, lineHeight: 1.2, color: 'var(--bone)', letterSpacing: '-0.01em' }}>
            {subtitle}
          </div>
        </div>
        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: 'var(--stone-2)', flex: '0 0 auto', whiteSpace: 'nowrap', paddingTop: 2 }}>
          in {inputCost} · out {outputCost}
        </div>
      </div>

      <TypingBlock tokens={tokens} reveal={reveal} mode={mode} phase={phase} onHover={onHover} eatenMap={eatenMap} />
    </div>
  );
}

// ------------------------------------------------------------
// MobilePipelineSlides — one full-width agent transcript at a time.
// Removed tokens remain crossed out after the animation so the reduction
// is still legible when users move backward through the slides.
// ------------------------------------------------------------
function MobilePipelineSlides({ task, mode, phases, reveals, eaten, slideIndex, setSlideIndex, onPrimary, onReplay }) {
  const hops = [
    { number: '01', role: 'Architect', subtitle: 'Chooses the approach', tokens: task.a1, output: task.a1Tokens },
    { number: '02', role: 'Builder', subtitle: 'Writes the implementation', tokens: task.a2, output: task.a2Tokens },
    { number: '03', role: 'Reviewer', subtitle: 'Flags risks and approves', tokens: task.a3, output: task.a3Tokens },
  ];

  const stateLabel = phase => {
    if (phase === 'done') return 'Reduced';
    if (phase === 'pending') return 'Waiting';
    if (phase === 'typing') return 'Writing';
    return 'Reducing';
  };

  const hop = hops[slideIndex];
  const phase = phases[slideIndex];
  const removedCharacters = hop.tokens.reduce((total, token) => (
    token.k === 'filler' || token.k === 'redundant' ? total + token.t.length : total
  ), 0);
  const reducedOutput = mode === 'optimized'
    ? Math.max(0, hop.output - Math.ceil(removedCharacters / 4))
    : hop.output;
  const nextRole = hops[slideIndex + 1]?.role;
  const primaryLabel = phase !== 'done'
    ? 'Show reduction'
    : nextRole ? `Next: ${nextRole}` : 'Replay demo';

  return (
    <section className="bv-mobile-slides" aria-label="Agent pipeline slides">
      <div className="bv-mobile-slide">
        <div className="bv-mobile-slide-topline">
          <span>{hop.number} / 03 · {hop.role}</span>
          <span className={`bv-mobile-slide-state is-${phase}`} aria-live="polite">{stateLabel(phase)}</span>
        </div>

        <div className="bv-mobile-slide-heading">
          <h3>{hop.subtitle}</h3>
          <span>One agent at a time</span>
        </div>

        <div className="bv-mobile-token-stats" aria-label="Output token reduction">
          <div><span>Original</span><strong>{hop.output.toLocaleString()}</strong></div>
          <span className="bv-mobile-token-arrow" aria-hidden="true">→</span>
          <div><span>After Brevitas</span><strong>{reducedOutput.toLocaleString()}</strong></div>
          <div className="bv-mobile-token-saved"><span>Removed</span><strong>−{(hop.output - reducedOutput).toLocaleString()}</strong></div>
        </div>

        <div className="bv-mobile-transcript">
          <TypingBlock
            tokens={hop.tokens}
            reveal={reveals[slideIndex]}
            mode={mode}
            phase={phase}
            eatenMap={eaten[slideIndex]}
            showRemoved
          />
        </div>

        <div className="bv-mobile-legend" aria-hidden="true">
          <span><i className="is-kept" />Kept</span>
          <span><i className="is-removed" />Removed</span>
        </div>

        <div className="bv-mobile-slide-nav" aria-label="Choose an agent slide">
          {hops.map((item, index) => (
            <button
              type="button"
              key={item.number}
              className={index === slideIndex ? 'is-active' : ''}
              aria-current={index === slideIndex ? 'step' : undefined}
              aria-label={`Show ${item.role} slide`}
              disabled={phases[index] === 'pending' && index > slideIndex}
              onClick={() => setSlideIndex(index)}
            >
              <span>{item.number}</span>{item.role}
            </button>
          ))}
        </div>

        <div className="bv-mobile-slide-actions">
          <button
            type="button"
            className="bv-mobile-back"
            disabled={slideIndex === 0}
            onClick={() => setSlideIndex(index => Math.max(0, index - 1))}
          >
            ← Back
          </button>
          <button
            type="button"
            className="bv-mobile-next"
            disabled={phase === 'pending'}
            onClick={phase === 'done' && !nextRole ? onReplay : onPrimary}
          >
            {primaryLabel} →
          </button>
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------
// CostReadout — right column. progress: 0..3 full-hop completions.
// ------------------------------------------------------------
function CostReadout({ task, mode, progress }) {
  const baselineCum = useMmPE(() => {
    if (progress <= 0) return 0;
    if (progress === 1) return task.baseline.call1;
    if (progress === 2) return task.baseline.call1 + task.baseline.call2;
    return task.baseline.total;
  }, [task, progress]);
  const optimizedCum = useMmPE(() => {
    if (progress <= 0) return 0;
    if (progress === 1) return task.optimized.call1;
    if (progress === 2) return task.optimized.call1 + task.optimized.call2;
    return task.optimized.total;
  }, [task, progress]);

  const showingOptimized = mode === 'optimized';
  const shownCum = showingOptimized ? optimizedCum : baselineCum;
  const saved = baselineCum - optimizedCum;
  const pct = baselineCum ? Math.round((saved / baselineCum) * 100) : 0;

  const Cell = ({ label, value, color, bold }) => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 9.5, letterSpacing: '0.14em', textTransform: 'uppercase', color: 'var(--stone-2)' }}>{label}</span>
      <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: bold ? 18 : 14, color: color || 'var(--bone)', fontVariantNumeric: 'tabular-nums' }}>{value}</span>
    </div>
  );

  return (
    <div className="bv-cost-readout" style={{
      background: 'var(--graphite)',
      padding: '15px 20px 14px',
      marginTop: 12,
      display: 'grid',
      gridTemplateColumns: 'auto 1fr auto auto auto auto',
      gap: 28,
      alignItems: 'center',
    }}>
      <div className="bv-cost-heading" style={{
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10.5,
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: 'var(--bronze)',
      }}>
        tokens in
      </div>

      {/* Hop progress bars — aligned under the hop cards above */}
      <div className="bv-cost-progress" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'JetBrains Mono, monospace', fontSize: 9, color: 'var(--stone)', letterSpacing: '0.12em' }}>
          <span>hop 1</span><span>hop 2</span><span>hop 3</span>
        </div>
        <div style={{ display: 'flex', gap: 10, height: 10 }}>
          {[1, 2, 3].map(n => {
            const b = task.baseline['call' + n];
            const o = task.optimized['call' + n];
            const pctBar = showingOptimized ? Math.round((o / b) * 100) : 100;
            const reached = progress >= n;
            return (
              <div key={n} style={{ flex: 1, background: 'rgba(166,159,147,0.15)', position: 'relative', overflow: 'hidden' }}>
                <div style={{
                  position: 'absolute',
                  inset: 0,
                  width: reached ? pctBar + '%' : 0,
                  background: 'var(--signal)',
                  transition: 'width 900ms cubic-bezier(.4,0,.2,1)',
                }} />
              </div>
            );
          })}
        </div>
      </div>

      <Cell label="without" value={baselineCum.toLocaleString()} color={showingOptimized ? 'var(--stone-2)' : 'var(--bone)'} />
      <Cell label="with brevitas" value={optimizedCum.toLocaleString()} color={showingOptimized ? 'var(--bone)' : 'var(--stone-2)'} />
      <Cell label="Saved" value={saved > 0 ? `−${saved.toLocaleString()}` : '—'} color="var(--signal)" />

      <div className="bv-cost-percent" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
        <div style={{ fontFamily: 'Newsreader, serif', fontSize: 40, lineHeight: 1, color: 'var(--signal)', letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums' }}>
          {pct}<span style={{ fontSize: 22, color: 'var(--stone-2)' }}>%</span>
        </div>
        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 9, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--stone-2)' }}>
          fewer tokens
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------
// PipelineFieldBg — ambient "design field" layer that sits behind the
// explorer content. Drifting dot grid + soft colored glow pulses +
// horizontal connection lines with traveling data packets.
// Pointer-events disabled; purely decorative.
// ------------------------------------------------------------
function PipelineFieldBg() {
  return (
    <div className="bv-field-wrap" aria-hidden="true">
      <div className="bv-field-grid" />
      <div className="bv-field-glow" />
      {/* Three horizontal "rails" roughly under the three hop cards */}
      <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}>
        <defs>
          <linearGradient id="bvRail" x1="0" x2="1">
            <stop offset="0" stopColor="rgba(166,159,147,0)" />
            <stop offset="0.15" stopColor="rgba(166,159,147,0.22)" />
            <stop offset="0.85" stopColor="rgba(166,159,147,0.22)" />
            <stop offset="1" stopColor="rgba(166,159,147,0)" />
          </linearGradient>
        </defs>
        {/* curved flow lines between hop slots */}
        <path d="M 8% 40% C 30% 40%, 35% 52%, 50% 52% S 70% 64%, 92% 64%"
              stroke="url(#bvRail)" strokeWidth="1" fill="none" strokeDasharray="2 6" opacity="0.5" />
        <path d="M 8% 72% C 30% 72%, 35% 58%, 50% 58% S 70% 44%, 92% 44%"
              stroke="url(#bvRail)" strokeWidth="1" fill="none" strokeDasharray="2 6" opacity="0.35" />
      </svg>
      {/* Traveling packets along an invisible path */}
      {[0, 1, 2, 3].map(i => (
        <div key={i} className="bv-field-packet" style={{
          offsetPath: 'path("M 0 120 C 300 120, 400 180, 600 180 S 900 260, 1200 260")',
          WebkitOffsetPath: 'path("M 0 120 C 300 120, 400 180, 600 180 S 900 260, 1200 260")',
          animationDelay: (i * 2.2) + 's',
          animationDuration: (7 + i * 1.5) + 's',
          opacity: 0,
        }} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------
// Main component — drives phases for all 3 hops with a simple
// state machine and timers.
// ------------------------------------------------------------
function PipelineExplorer({ defaultMode = 'optimized' }) {
  const [activeId, setActiveId] = usePE('rate-limiter');
  const [mode, setMode] = usePE(defaultMode);
  const [hover, setHover] = usePE(null);

  // Active hop (0, 1, 2) — the one currently typing or compressing
  const [activeHop, setActiveHop] = usePE(0);
  // Reveal count for the active hop's typing phase
  const [reveal, setReveal] = usePE(0);
  // Per-hop phase: 'pending' | 'typing' | 'highlighting' | 'deleting' | 'done'
  const [phases, setPhases] = usePE(['pending', 'pending', 'pending']);
  // Per-hop per-token char-erase counts: { [tokenIndex]: charsEatenFromRight }
  const [eaten, setEaten] = usePE([{}, {}, {}]);
  const [runToken, setRunToken] = usePE(0); // bumps on reset to cancel old timers
  const [startHop, setStartHop] = usePE(0); // which hop to start the run at (0..2)
  const [isVisible, setIsVisible] = usePE(false);
  const [mobileSlide, setMobileSlide] = usePE(0);

  // Imperative hooks for keyboard skip/back — updated by the run loop
  const skipRef = useRfPE({ finishCurrent: () => {}, advanceHop: () => {} });
  const rootRef = useRfPE(null);

  const task = useMmPE(() => TASKS.find(t => t.id === activeId), [activeId]);

  // ---------- Run pipeline ----------
  // Each time runToken or activeId or mode changes, kick off a fresh run.
  useEfPE(() => {
    let cancelled = false;
    let skipCurrent = false;   // finish typing + deletion for current hop instantly
    let advanceNow = false;    // skip to next hop
    const timers = [];
    const push = (fn, ms) => {
      const id = setTimeout(() => { if (!cancelled) fn(); }, ms);
      timers.push(id);
    };
    const wait = (ms) => new Promise(r => push(r, ms));

    // Expose skip/advance to the keyboard handler
    skipRef.current = {
      finishCurrent: () => { skipCurrent = true; },
      advanceHop: () => { advanceNow = true; skipCurrent = true; },
    };

    async function runHop(h) {
      if (cancelled) return;
      skipCurrent = false;
      advanceNow = false;
      const hopTokens = [task.a1, task.a2, task.a3][h];
      setActiveHop(h);
      setReveal(0);
      setPhases(p => { const n = [...p]; n[h] = 'typing'; return n; });
      // Clear eaten state for this hop
      setEaten(e => { const n = [...e]; n[h] = {}; return n; });

      // Type out word by word
      const total = hopTokens.length;
      const perTok = Math.max(42, Math.min(85, 7500 / total));
      for (let i = 0; i <= total; i++) {
        if (cancelled) return;
        if (skipCurrent) { setReveal(total); break; }
        setReveal(i);
        await wait(perTok);
      }
      if (cancelled) return;

      if (mode === 'optimized') {
        // Highlighting phase — quick flash of red to mark all droppable tokens
        setPhases(p => { const n = [...p]; n[h] = 'highlighting'; return n; });
        await wait(skipCurrent ? 250 : 950);
        if (cancelled) return;

        // Deleting phase — CHAR-BY-CHAR backspace in a wave, ~1.1s total
        setPhases(p => { const n = [...p]; n[h] = 'deleting'; return n; });

        // Collect droppable token indices in reading order
        const droppable = [];
        for (let idx = 0; idx < hopTokens.length; idx++) {
          const k = hopTokens[idx].k;
          if (k === 'filler' || k === 'redundant') droppable.push(idx);
        }

        if (droppable.length > 0) {
          // Target: finish all deletions in ~1000ms, with a wavy overlap so you
          // see multiple words being backspaced at once.
          const targetMs = skipCurrent ? 180 : 1350;
          const maxLen = Math.max(...droppable.map(i => hopTokens[i].t.length));
          // Start-stagger between words: small to make the wave feel dense & urgent
          const staggerStep = Math.max(24, (targetMs - 200) / Math.max(1, droppable.length));
          // Char tick: backspace speed
          const charTick = Math.max(28, Math.min(72, 720 / maxLen));

          // Each word's animation: start at its stagger offset, then tick each char
          const wordPromises = droppable.map((tokIdx, orderI) => new Promise((resolve) => {
            const len = hopTokens[tokIdx].t.length;
            const startDelay = orderI * staggerStep;
            const startId = setTimeout(() => {
              if (cancelled) return resolve();
              let step = 0;
              const iv = setInterval(() => {
                if (cancelled || skipCurrent) {
                  clearInterval(iv);
                  setEaten(e => {
                    const n = [...e];
                    n[h] = { ...n[h], [tokIdx]: len };
                    return n;
                  });
                  return resolve();
                }
                step++;
                setEaten(e => {
                  const n = [...e];
                  n[h] = { ...n[h], [tokIdx]: step };
                  return n;
                });
                if (step >= len) {
                  clearInterval(iv);
                  resolve();
                }
              }, charTick);
              timers.push(iv);
            }, startDelay);
            timers.push(startId);
          }));

          await Promise.all(wordPromises);
        }

        if (cancelled) return;
        await wait(skipCurrent ? 80 : 380);
      }

      setPhases(p => { const n = [...p]; n[h] = 'done'; return n; });
      // Small pause between hops — but if user pressed →, skip it
      await wait(advanceNow ? 0 : (skipCurrent ? 150 : 650));
    }

    (async () => {
      // Pre-fill any hops before startHop as 'done' so the UI shows the
      // state you'd be in if those had already played.
      const initPhases = ['pending', 'pending', 'pending'];
      const initEaten = [{}, {}, {}];
      const hopTokArr = [task.a1, task.a2, task.a3];
      for (let i = 0; i < startHop; i++) {
        initPhases[i] = 'done';
        if (mode === 'optimized') {
          // Mark all droppable tokens as fully eaten
          const map = {};
          hopTokArr[i].forEach((tk, idx) => {
            if (tk.k === 'filler' || tk.k === 'redundant') map[idx] = tk.t.length;
          });
          initEaten[i] = map;
        }
      }
      setPhases(initPhases);
      setEaten(initEaten);
      setReveal(startHop < 3 ? 0 : hopTokArr[2].length);
      setActiveHop(Math.min(startHop, 2));
      await wait(250);
      for (let h = startHop; h < 3; h++) {
        if (cancelled) return;
        await runHop(h);
      }
    })();

    return () => {
      cancelled = true;
      timers.forEach(id => { clearTimeout(id); clearInterval(id); });
    };
  }, [activeId, mode, runToken, task]);

  // Per-hop reveal counts (only the active hop is actively advancing)
  const reveals = useMmPE(() => {
    const lens = [task.a1.length, task.a2.length, task.a3.length];
    return [0, 1, 2].map(h => {
      if (phases[h] === 'pending') return 0;
      if (phases[h] === 'typing')  return h === activeHop ? reveal : 0;
      return lens[h]; // highlighting/deleting/done: all tokens placed
    });
  }, [phases, activeHop, reveal, task]);

  // Cost progress — hop counts as "sent" once it starts typing
  const costProgress = useMmPE(() => {
    let p = 0;
    for (let h = 0; h < 3; h++) {
      if (phases[h] !== 'pending') p = h + 1;
    }
    return p;
  }, [phases]);

  // Scroll focused hop into view horizontally WITHOUT affecting the page's
  // vertical scroll position (scrollIntoView with block:'nearest' can still
  // pull the page up/down when the strip is partially off-screen).
  const stripRef = useRfPE(null);
  useEfPE(() => {
    const container = stripRef.current;
    const el = container?.children[activeHop];
    if (!container || !el) return;
    const targetLeft = el.offsetLeft - (container.clientWidth - el.offsetWidth) / 2;
    container.scrollTo({ left: Math.max(0, targetLeft), behavior: 'smooth' });
  }, [activeHop]);

  function pickTask(id) {
    setActiveId(id);
    setMobileSlide(0);
    setStartHop(0);
    setRunToken(n => n + 1);
  }
  function replay() {
    setMobileSlide(0);
    setStartHop(0);
    setRunToken(n => n + 1);
  }
  function skipToEnd() {
    setRunToken(n => n + 10000); // cancel
    setMobileSlide(2);
    setPhases(['done', 'done', 'done']);
    // Mark all droppable tokens as fully eaten so the final state looks correct
    if (mode === 'optimized') {
      const finalEaten = [task.a1, task.a2, task.a3].map(toks => {
        const map = {};
        toks.forEach((tk, idx) => {
          if (tk.k === 'filler' || tk.k === 'redundant') map[idx] = tk.t.length;
        });
        return map;
      });
      setEaten(finalEaten);
    }
  }
  function switchMode(m) {
    setMode(m);
    setMobileSlide(0);
    setStartHop(0);
    setRunToken(n => n + 1);
  }
  function advanceMobileSlide() {
    if (phases[mobileSlide] !== 'done') {
      skipRef.current.finishCurrent();
      return;
    }
    const nextSlide = Math.min(2, mobileSlide + 1);
    setMobileSlide(nextSlide);
    setStartHop(nextSlide);
    setRunToken(n => n + 1);
  }

  // ---- Visibility gating for arrow keys ----
  useEfPE(() => {
    if (!rootRef.current) return;
    const obs = new IntersectionObserver((entries) => {
      entries.forEach(e => setIsVisible(e.isIntersecting && e.intersectionRatio > 0.3));
    }, { threshold: [0, 0.3, 0.6, 1] });
    obs.observe(rootRef.current);
    return () => obs.disconnect();
  }, []);

  // ---- Keyboard handler ----
  useEfPE(() => {
    if (!isVisible) return;
    function onKey(ev) {
      // Ignore when focus is in an input/textarea/contenteditable
      const t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;

      if (ev.key === 'ArrowRight') {
        ev.preventDefault();
        // If all hops are done, do nothing
        const allDone = phases.every(p => p === 'done');
        if (allDone) return;
        // If the active hop is mid-typing, finish typing first (one press = finish current step).
        // If the active hop is fully done ('done'), advance to next hop (the run loop already advances on its own,
        // but pressing here shortcuts the inter-hop pause).
        const curPhase = phases[activeHop];
        if (curPhase === 'typing' || curPhase === 'highlighting' || curPhase === 'deleting') {
          skipRef.current.finishCurrent();
        } else {
          skipRef.current.advanceHop();
        }
      } else if (ev.key === 'ArrowLeft') {
        ev.preventDefault();
        // Step back to the previous hop. If we're on hop 0, restart the task.
        const back = Math.max(0, activeHop - 1);
        setStartHop(back);
        setRunToken(n => n + 1);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isVisible, phases, activeHop]);

  return (
    <div ref={rootRef} className="bv-explorer" style={{ width: '100%', position: 'relative' }}>
      <PipelineFieldBg />
      <style>{`
        @keyframes bvCursorBlink { 0%, 49% { opacity: 1 } 50%, 100% { opacity: 0 } }
        .bv-ctl-btn { transition: border-color 160ms, color 160ms, background 160ms, transform 120ms; white-space: nowrap; }
        .bv-ctl-btn:hover { border-color: var(--bronze) !important; background: var(--component-bg-dark-light) !important; }
        .bv-ctl-btn:active { transform: translateY(1px); }
        .bv-ctl-btn:focus-visible, .bv-task-pill:focus-visible { outline: 2px solid var(--bronze); outline-offset: 3px; }
        .bv-task-pill { transition: color 160ms, background 160ms; }
        .bv-task-pill:hover { color: var(--bone); background: var(--component-bg-dark-light); }
        .bv-task-tabs, .bv-prompt, .bv-pipe-grid, .bv-mobile-slides, .bv-cost-readout { position: relative; z-index: 1; }
        .bv-keyboard-hint {
          display: inline-flex; align-items: center; gap: 5px;
          color: var(--stone-2); font-family: 'JetBrains Mono', monospace;
          font-size: 10px; letter-spacing: 0.06em; white-space: nowrap;
        }
        .bv-keyboard-hint kbd {
          min-width: 25px; height: 25px; display: inline-flex; align-items: center; justify-content: center;
          border: 1px solid var(--line); border-bottom-color: var(--stone);
          border-radius: 5px; background: var(--graphite); color: var(--bone);
          font-family: inherit; font-size: 12px; line-height: 1;
        }
        .bv-strip > div + div { border-left: 1px solid var(--line); }
        .bv-strip { scrollbar-width: thin; scrollbar-color: var(--stone) transparent; }
        .bv-strip::-webkit-scrollbar { height: 6px }
        .bv-strip::-webkit-scrollbar-track { background: transparent }
        .bv-strip::-webkit-scrollbar-thumb { background: var(--stone); border-radius: 4px }
        .bv-mobile-slides { display: none; }
        @media (max-width: 900px) {
          .bv-prompt { flex-wrap: wrap; align-items: flex-start !important; }
          .bv-prompt-actions { width: 100%; justify-content: flex-end; }
          .bv-keyboard-hint { display: none; }
          .bv-pipe-grid { overflow-x: auto; overscroll-behavior-inline: contain; }
          .bv-strip { min-width: 840px; }
        }
        @media (max-width: 640px) {
          .bv-pipe-grid { display: none; }
          .bv-mobile-slides {
            display: block;
            margin-top: 14px;
            margin-left: calc(-18px - env(safe-area-inset-left));
            width: calc(100% + 36px + env(safe-area-inset-left) + env(safe-area-inset-right));
            border-top: 1px solid var(--line);
            border-bottom: 1px solid var(--line);
            background: var(--graphite);
          }
          .bv-mobile-slide {
            min-height: 72svh;
            padding: 22px calc(18px + env(safe-area-inset-right)) 20px calc(18px + env(safe-area-inset-left));
          }
          .bv-mobile-slide-topline,
          .bv-mobile-slide-heading > span,
          .bv-mobile-token-stats span,
          .bv-mobile-legend,
          .bv-mobile-slide-nav button,
          .bv-mobile-slide-actions button {
            font-family: 'JetBrains Mono', monospace;
            text-transform: uppercase;
          }
          .bv-mobile-slide-topline {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: var(--bronze);
            font-size: 10px;
            letter-spacing: 0.12em;
          }
          .bv-mobile-slide-state { color: var(--stone-2); }
          .bv-mobile-slide-state.is-typing,
          .bv-mobile-slide-state.is-highlighting,
          .bv-mobile-slide-state.is-deleting { color: var(--signal); }
          .bv-mobile-slide-state.is-done { color: var(--bone); }
          .bv-mobile-slide-heading { margin: 18px 0; }
          .bv-mobile-slide-heading h3 {
            margin: 0 0 5px;
            color: var(--bone);
            font-family: 'Newsreader', serif;
            font-size: clamp(28px, 9vw, 38px);
            font-weight: 400;
            line-height: 1.02;
            letter-spacing: -0.02em;
          }
          .bv-mobile-slide-heading > span { color: var(--stone-2); font-size: 9px; letter-spacing: 0.1em; }
          .bv-mobile-token-stats {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
            gap: 8px 12px;
            align-items: center;
            padding: 14px;
            border: 1px solid var(--line);
            background: var(--component-bg-dark);
          }
          .bv-mobile-token-stats > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
          .bv-mobile-token-stats span { color: var(--stone-2); font-size: 8px; letter-spacing: 0.1em; }
          .bv-mobile-token-stats strong {
            color: var(--bone);
            font-family: 'JetBrains Mono', monospace;
            font-size: 18px;
            font-weight: 500;
            font-variant-numeric: tabular-nums;
          }
          .bv-mobile-token-arrow { color: var(--bronze) !important; font-size: 18px !important; }
          .bv-mobile-token-saved {
            grid-column: 1 / -1;
            flex-direction: row !important;
            justify-content: space-between;
            align-items: baseline;
            padding-top: 9px;
            border-top: 1px solid var(--line);
          }
          .bv-mobile-token-saved strong { color: var(--signal); }
          .bv-mobile-transcript {
            height: clamp(330px, 50svh, 520px);
            margin: 14px 0 10px;
            padding: 18px;
            overflow-y: auto;
            overscroll-behavior: contain;
            border: 1px solid var(--line);
            background: var(--component-bg-dark-light);
            scrollbar-width: thin;
            scrollbar-color: var(--stone) transparent;
          }
          .bv-mobile-transcript .bv-transcript { font-size: 17px !important; line-height: 1.7 !important; }
          .bv-token-removed {
            color: var(--oxblood) !important;
            text-decoration: line-through;
            text-decoration-thickness: 1px;
            opacity: 0.62;
          }
          .bv-mobile-legend { display: flex; gap: 18px; color: var(--stone-2); font-size: 8px; letter-spacing: 0.1em; }
          .bv-mobile-legend span { display: inline-flex; align-items: center; gap: 6px; }
          .bv-mobile-legend i { width: 7px; height: 7px; border-radius: 50%; background: var(--bone); }
          .bv-mobile-legend i.is-removed { background: var(--oxblood); }
          .bv-mobile-slide-nav { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; margin-top: 18px; }
          .bv-mobile-slide-nav button {
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 3px;
            min-width: 0;
            padding: 9px;
            border: 1px solid var(--line);
            border-radius: 4px;
            background: transparent;
            color: var(--stone-2);
            font-size: 8px;
            letter-spacing: 0.05em;
            text-align: left;
          }
          .bv-mobile-slide-nav button span { color: var(--bronze); font-size: 9px; }
          .bv-mobile-slide-nav button.is-active { border-color: var(--bronze); color: var(--bone); background: var(--component-bg-dark-light); }
          .bv-mobile-slide-nav button:disabled { opacity: 0.38; }
          .bv-mobile-slide-actions { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px; margin-top: 10px; }
          .bv-mobile-slide-actions button {
            padding: 12px 14px;
            border: 1px solid var(--stone);
            border-radius: 5px;
            font-size: 10px;
            letter-spacing: 0.07em;
          }
          .bv-mobile-back { background: transparent; color: var(--stone-2); }
          .bv-mobile-next { background: var(--signal); border-color: var(--signal) !important; color: var(--ink); }
          .bv-mobile-slide-actions button:disabled { opacity: 0.38; }
          .bv-prompt {
            align-items: stretch !important;
            flex-direction: column;
            gap: 12px !important;
          }
          .bv-prompt-actions {
            display: none !important;
          }
          .bv-keyboard-hint { display: none; }
          .bv-prompt-actions .bv-ctl-btn { width: 100%; }
          .section .bv-cost-readout {
            grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
            gap: 18px 16px !important;
            padding: 18px !important;
          }
          .bv-cost-heading,
          .bv-cost-progress { grid-column: 1 / -1; }
          .bv-cost-percent { align-items: flex-start !important; }
        }
        @keyframes bvFieldDrift { 0% { transform: translate(0,0) } 100% { transform: translate(-24px, -24px) } }
        @keyframes bvPulse { 0%, 100% { opacity: 0.25 } 50% { opacity: 0.75 } }
        @keyframes bvPacket { 0% { offset-distance: 0%; opacity: 0 } 10% { opacity: 1 } 90% { opacity: 1 } 100% { offset-distance: 100%; opacity: 0 } }
        .bv-field-wrap { position: absolute; inset: -40px -24px; pointer-events: none; z-index: 0; overflow: hidden; border-radius: 8px; }
        .bv-field-grid {
          position: absolute; inset: -24px;
          background-image:
            radial-gradient(circle at 1px 1px, rgba(166,159,147,0.11) 1px, transparent 1px);
          background-size: 24px 24px;
          animation: bvFieldDrift 36s linear infinite;
          mask-image: radial-gradient(ellipse 85% 70% at 50% 50%, black 40%, transparent 95%);
          -webkit-mask-image: radial-gradient(ellipse 85% 70% at 50% 50%, black 40%, transparent 95%);
        }
        .bv-field-glow {
          position: absolute; left: 50%; top: 50%;
          width: 900px; height: 560px; transform: translate(-50%, -50%);
          background: radial-gradient(ellipse at 20% 50%, rgba(138,98,66,0.06), transparent 60%),
                      radial-gradient(ellipse at 80% 50%, rgba(122,180,106,0.04), transparent 60%);
          filter: blur(12px);
          animation: bvPulse 10s ease-in-out infinite;
          opacity: 0.8;
        }
        .bv-field-line {
          position: absolute; left: 0; right: 0; height: 1px;
          background: linear-gradient(90deg, transparent, rgba(166,159,147,0.18) 20%, rgba(166,159,147,0.18) 80%, transparent);
        }
        /* Hide moving green packets to reduce visual noise */
        .bv-field-packet {
          display: none !important;
          /* fallback: keep size but invisible if needed */
          width: 6px; height: 6px; border-radius: 50%;
          background: transparent;
          box-shadow: none;
          animation: none !important;
        }
        /* Ambient glow removed — no decorative gradients behind the tool */
        .bv-field-glow { display: none !important; }
        @media (prefers-reduced-motion: reduce) {
          .bv-field-grid, .bv-field-packet { animation: none !important; }
          .bv-transcript span { animation-duration: 1ms !important; }
        }
      `}</style>


      {/* Task tabs — active one is a quiet chip with a bronze underline */}
      <div className="bv-task-tabs" role="group" aria-label="Choose a demo task" style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', marginBottom: 14 }}>
        {TASKS.map(t => {
          const active = t.id === activeId;
          return (
            <button key={t.id} className="bv-task-pill" aria-pressed={active} onClick={() => pickTask(t.id)}
              style={{
                background: active ? 'var(--component-bg-dark-light)' : 'transparent',
                color: active ? 'var(--bone)' : 'var(--stone-2)',
                border: 'none',
                boxShadow: active ? 'inset 0 -1.5px 0 var(--bronze)' : 'none',
                padding: '9px 14px', fontFamily: 'JetBrains Mono, monospace',
                fontSize: 12.5, letterSpacing: '0.025em', borderRadius: 5, cursor: 'pointer',
                fontWeight: active ? 600 : 400,
              }}>{t.label}</button>
          );
        })}
      </div>

      {/* User prompt — the request everything below is answering; a quiet surface, no outline */}
      <div className="bv-prompt" style={{
        background: 'var(--component-bg-dark-light)',
        padding: '16px 18px', borderRadius: 7, marginBottom: 14,
        display: 'flex', alignItems: 'center', gap: 16,
      }}>
        <div style={{ fontFamily: 'Newsreader, serif', fontSize: 19, lineHeight: 1.4, color: 'var(--bone)', flex: 1, letterSpacing: '-0.005em' }}>
          {task.user}
        </div>
        <div className="bv-prompt-actions" style={{ display: 'flex', gap: 8, flex: '0 0 auto', alignItems: 'center' }}>
          <span className="bv-keyboard-hint" aria-label="Use the left and right arrow keys to step through the animation">
            <kbd>←</kbd><kbd>→</kbd><span>step</span>
          </span>
          <button onClick={replay} className="bv-ctl-btn" style={btnStyle} aria-label="Replay the animation">↻ Replay</button>
          <button onClick={skipToEnd} className="bv-ctl-btn" style={btnStyle} aria-label="Skip the animation and show the final result">Skip to result →</button>
        </div>
      </div>

      {/* Main grid — cost readout moved BELOW so three hops can breathe */}
      <div style={{ position: 'relative' }} className="bv-pipe-grid">
        <div ref={stripRef} className="bv-strip" style={{
          display: 'flex', gap: 0, minWidth: 0, alignItems: 'stretch',
        }}>
          {[
            { role: '01 · ARCHITECT', subtitle: 'Chooses the approach', toks: task.a1, out: task.a1Tokens,
              inB: task.baseline.call1, inO: task.optimized.call1 },
            { role: '02 · BUILDER', subtitle: 'Writes the implementation', toks: task.a2, out: task.a2Tokens,
              inB: task.baseline.call2, inO: task.optimized.call2 },
            { role: '03 · REVIEWER', subtitle: 'Flags risks · approves', toks: task.a3, out: task.a3Tokens,
              inB: task.baseline.call3, inO: task.optimized.call3 },
          ].map((h, i) => (
            <HopCard key={i}
              role={h.role} subtitle={h.subtitle}
              tokens={h.toks} reveal={reveals[i]} mode={mode} phase={phases[i]}
              onHover={setHover}
              eatenMap={eaten[i]}
              inputCost={(mode === 'optimized' ? h.inO : h.inB).toLocaleString()}
              outputCost={h.out.toLocaleString()}
            />
          ))}
        </div>
      </div>

      <MobilePipelineSlides
        task={task}
        mode={mode}
        phases={phases}
        reveals={reveals}
        eaten={eaten}
        slideIndex={mobileSlide}
        setSlideIndex={setMobileSlide}
        onPrimary={advanceMobileSlide}
        onReplay={replay}
      />
      <CostReadout task={task} mode={mode} progress={costProgress} />


      {/* Hover tooltip */}
      {hover && hover.tok && dropReason[hover.tok.k] && (
        <div style={{
          marginTop: 12, padding: '10px 14px', background: 'var(--component-bg-dark)',
          borderRadius: 6,
          fontFamily: 'Newsreader, serif', fontSize: 13.5, color: 'var(--stone-2)', fontStyle: 'italic',
        }}>
          <span style={{
            fontFamily: 'JetBrains Mono, monospace', fontSize: 10, letterSpacing: '0.14em',
            color: 'var(--bronze)', fontStyle: 'normal', textTransform: 'uppercase', marginRight: 10,
          }}>{hover.tok.k}</span>
          {dropReason[hover.tok.k]}
        </div>
      )}
    </div>
  );
}

const btnStyle = {
  background: 'var(--graphite)', border: '1px solid var(--stone)', color: 'var(--bone)',
  padding: '8px 15px', fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5,
  letterSpacing: '0.08em', cursor: 'pointer', borderRadius: 5, textTransform: 'uppercase',
  fontWeight: 500,
};

const kbdStyle = {
  display: 'inline-block',
  padding: '2px 6px',
  border: '1px solid currentColor',
  borderRadius: 2,
  fontFamily: 'JetBrains Mono, monospace',
  fontSize: 10,
  lineHeight: 1,
  marginRight: 2,
};

Object.assign(window, { PipelineExplorer });
