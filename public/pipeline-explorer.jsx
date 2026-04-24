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
function PToken({ tok, mode, phase, onHover, charEaten }) {
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
        padding: highlighted ? '1px 3px' : 0,
        borderRadius: highlighted ? 2 : 0,
        transition: 'color 250ms, background 250ms, padding 250ms',
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
function TypingBlock({ tokens, reveal, mode, phase, onHover, eatenMap }) {
  const shown = tokens.slice(0, reveal);
  const typing = phase === 'typing' && reveal < tokens.length;
  return (
    <div style={{
      fontFamily: 'Newsreader, serif',
      fontSize: 14.5,
      lineHeight: 1.75,
      color: 'var(--bone)',
      minHeight: 200,
      maxHeight: 260,
      overflowY: 'auto',
    }}>
      {shown.map((tok, i) => (
        <React.Fragment key={i}>
          <PToken tok={tok} mode={mode} phase={phase} onHover={onHover} charEaten={eatenMap ? eatenMap[i] : -1} />
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
  );
}

// ------------------------------------------------------------
// HopCard — one agent hop. Uses hop phase for styling.
// ------------------------------------------------------------
function HopCard({ role, subtitle, tokens, reveal, mode, phase, onHover, inputCost, outputCost, eatenMap }) {
  const active = phase === 'typing' || phase === 'highlighting' || phase === 'deleting';
  const done = phase === 'done';
  const pending = phase === 'pending';

  let statusText = '○ PENDING';
  let statusColor = 'var(--stone)';
  if (phase === 'typing')      { statusText = '● GENERATING';  statusColor = 'var(--signal)'; }
  else if (phase === 'highlighting') { statusText = '◐ MARKING DROPS'; statusColor = 'var(--oxblood)'; }
  else if (phase === 'deleting')     { statusText = '◑ COMPRESSING';  statusColor = 'var(--bronze)'; }
  else if (phase === 'done')         { statusText = '✓ DONE';  statusColor = 'var(--bone-dim)'; }

  return (
    <div style={{
      flex: '1 1 0',
      minWidth: 0,
      border: active ? '1px solid var(--bronze)' : '1px solid var(--line)',
      boxShadow: active ? '0 0 0 3px rgba(138,98,66,0.12)' : 'none',
      background: pending ? 'rgba(16,15,13,0.4)' : 'var(--graphite)',
      borderRadius: 4,
      padding: '22px 24px 20px',
      transition: 'border-color 400ms, box-shadow 400ms, background 400ms, opacity 400ms',
      opacity: pending ? 0.45 : 1,
      position: 'relative',
      display: 'flex',
      flexDirection: 'column',
      gap: 14,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 10.5,
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
            color: active ? 'var(--bronze)' : 'var(--stone-2)',
            marginBottom: 4,
            transition: 'color 300ms',
          }}>
            {role}
          </div>
          <div style={{ fontFamily: 'Newsreader, serif', fontSize: 18, color: 'var(--bone)', letterSpacing: '-0.01em' }}>
            {subtitle}
          </div>
        </div>
        <div style={{ textAlign: 'right', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
          <div style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 9.5,
            letterSpacing: '0.1em',
            color: statusColor,
            transition: 'color 300ms',
          }}>
            {statusText}
          </div>
          <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: 'var(--stone-2)' }}>
            in {inputCost} · out {outputCost}
          </div>
        </div>
      </div>

      <TypingBlock tokens={tokens} reveal={reveal} mode={mode} phase={phase} onHover={onHover} eatenMap={eatenMap} />
    </div>
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
    <div style={{
      border: '1px solid var(--line)',
      background: 'var(--graphite)',
      padding: '18px 22px 16px',
      borderRadius: 4,
      marginTop: 14,
      display: 'grid',
      gridTemplateColumns: 'auto 1fr auto auto auto auto',
      gap: 28,
      alignItems: 'center',
    }}>
      <div style={{
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10.5,
        letterSpacing: '0.14em',
        textTransform: 'uppercase',
        color: 'var(--bronze)',
      }}>
        Input tokens<br/>burned
      </div>

      {/* Hop progress bars — aligned under the hop cards above */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'JetBrains Mono, monospace', fontSize: 9, color: 'var(--stone)', letterSpacing: '0.12em' }}>
          <span>HOP 1</span><span>HOP 2</span><span>HOP 3</span>
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

      <Cell label="Baseline" value={baselineCum.toLocaleString()} color={showingOptimized ? 'var(--stone-2)' : 'var(--bone)'} />
      <Cell label="Brevitas" value={optimizedCum.toLocaleString()} color={showingOptimized ? 'var(--bone)' : 'var(--stone-2)'} />
      <Cell label="Saved" value={saved > 0 ? `−${saved.toLocaleString()}` : '—'} color="var(--signal)" />

      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
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
      const perTok = Math.max(32, Math.min(65, 5500 / total));
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
        await wait(skipCurrent ? 250 : 700);
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
          const targetMs = skipCurrent ? 180 : 1000;
          const maxLen = Math.max(...droppable.map(i => hopTokens[i].t.length));
          // Start-stagger between words: small to make the wave feel dense & urgent
          const staggerStep = Math.max(18, (targetMs - 200) / Math.max(1, droppable.length));
          // Char tick: fast backspace
          const charTick = Math.max(22, Math.min(55, 550 / maxLen));

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
        await wait(skipCurrent ? 80 : 280);
      }

      setPhases(p => { const n = [...p]; n[h] = 'done'; return n; });
      // Small pause between hops — but if user pressed →, skip it
      await wait(advanceNow ? 0 : (skipCurrent ? 150 : 500));
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
    setStartHop(0);
    setRunToken(n => n + 1);
  }
  function replay() { setStartHop(0); setRunToken(n => n + 1); }
  function skipToEnd() {
    setRunToken(n => n + 10000); // cancel
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
    setStartHop(0);
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
    <div ref={rootRef} style={{ width: '100%', position: 'relative' }}>
      <PipelineFieldBg />
      <style>{`
        @keyframes bvCursorBlink { 0%, 49% { opacity: 1 } 50%, 100% { opacity: 0 } }
        .bv-task-pill { transition: all 200ms; }
        .bv-task-pill:hover { border-color: var(--bronze); color: var(--bone); }
        .bv-strip { scrollbar-width: thin; scrollbar-color: var(--stone) transparent; }
        .bv-strip::-webkit-scrollbar { height: 6px }
        .bv-strip::-webkit-scrollbar-track { background: transparent }
        .bv-strip::-webkit-scrollbar-thumb { background: var(--stone); border-radius: 4px }
        @media (max-width: 900px) {
          .bv-pipe-grid { grid-template-columns: 1fr !important; }
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
          background: radial-gradient(ellipse at 20% 50%, rgba(138,98,66,0.08), transparent 60%),
                      radial-gradient(ellipse at 80% 50%, rgba(122,180,106,0.06), transparent 60%);
          filter: blur(20px);
          animation: bvPulse 8s ease-in-out infinite;
        }
        .bv-field-line {
          position: absolute; left: 0; right: 0; height: 1px;
          background: linear-gradient(90deg, transparent, rgba(166,159,147,0.18) 20%, rgba(166,159,147,0.18) 80%, transparent);
        }
        .bv-field-packet {
          position: absolute; width: 6px; height: 6px; border-radius: 50%;
          background: var(--signal);
          box-shadow: 0 0 8px var(--signal), 0 0 16px rgba(122,180,106,0.4);
          animation: bvPacket 9s linear infinite;
        }
      `}</style>

      {/* Task pills */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', marginBottom: 18 }}>
        <div style={{
          fontFamily: 'JetBrains Mono, monospace', fontSize: 10, letterSpacing: '0.16em',
          textTransform: 'uppercase', color: 'var(--stone-2)', marginRight: 4,
        }}>Try a task →</div>
        {TASKS.map(t => {
          const active = t.id === activeId;
          return (
            <button key={t.id} className="bv-task-pill" onClick={() => pickTask(t.id)}
              style={{
                background: active ? 'var(--bronze)' : 'transparent',
                color: active ? 'var(--obsidian)' : 'var(--stone-2)',
                border: '1px solid ' + (active ? 'var(--bronze)' : 'var(--line)'),
                padding: '7px 14px', fontFamily: 'JetBrains Mono, monospace',
                fontSize: 11, letterSpacing: '0.06em', borderRadius: 2, cursor: 'pointer',
              }}>{t.label}</button>
          );
        })}
      </div>

      {/* User prompt bar */}
      <div style={{
        border: '1px solid var(--line)', background: 'rgba(16,15,13,0.5)',
        padding: '16px 20px', borderRadius: 4, marginBottom: 18,
        display: 'flex', alignItems: 'center', gap: 16,
      }}>
        <div style={{
          fontFamily: 'JetBrains Mono, monospace', fontSize: 10, letterSpacing: '0.14em',
          color: 'var(--bronze)', flex: '0 0 auto', textTransform: 'uppercase',
        }}>USER →</div>
        <div style={{ fontFamily: 'Newsreader, serif', fontSize: 17, color: 'var(--bone)', flex: 1, letterSpacing: '-0.005em' }}>
          {task.user}
        </div>
        <div style={{ display: 'flex', gap: 8, flex: '0 0 auto', alignItems: 'center' }}>
          <span style={{
            fontFamily: 'JetBrains Mono, monospace', fontSize: 9.5, letterSpacing: '0.1em',
            color: isVisible ? 'var(--bronze)' : 'var(--stone)',
            textTransform: 'uppercase', marginRight: 4,
            transition: 'color 200ms',
            display: 'inline-flex', alignItems: 'center', gap: 6,
          }} title={isVisible ? 'Arrow keys active' : 'Scroll into view to use arrow keys'}>
            <kbd style={kbdStyle}>→</kbd>
            <span style={{ opacity: 0.85 }}>to finish current</span>
          </span>
          <button onClick={replay} style={btnStyle}>↻ Replay</button>
          <button onClick={skipToEnd} style={btnStyle}>⇥ Skip</button>
        </div>
      </div>

      {/* Main grid — cost readout moved BELOW so three hops can breathe */}
      <div style={{ position: 'relative' }} className="bv-pipe-grid">
        <div ref={stripRef} className="bv-strip" style={{
          display: 'flex', gap: 10, minWidth: 0, alignItems: 'stretch',
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

      <CostReadout task={task} mode={mode} progress={costProgress} />

      {/* Mode toggle + legend */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginTop: 22, paddingTop: 18, borderTop: '1px solid var(--line)',
        gap: 18, flexWrap: 'wrap',
      }}>
        <div style={{ display: 'flex', gap: 0, border: '1px solid var(--line)', borderRadius: 2 }}>
          {[
            { k: 'baseline', label: 'Raw (no layer)' },
            { k: 'optimized', label: 'With Brevitas' },
          ].map(({ k, label }) => {
            const active = mode === k;
            return (
              <button key={k} onClick={() => switchMode(k)} style={{
                padding: '7px 16px', fontFamily: 'JetBrains Mono, monospace',
                fontSize: 10.5, letterSpacing: '0.12em', textTransform: 'uppercase',
                background: active ? 'var(--bronze)' : 'transparent',
                color: active ? 'var(--obsidian)' : 'var(--stone-2)',
                border: 'none', cursor: 'pointer', transition: 'all 200ms',
              }}>{label}</button>
            );
          })}
        </div>
        <div style={{
          display: 'flex', gap: 18, fontFamily: 'JetBrains Mono, monospace', fontSize: 10,
          color: 'var(--stone-2)', letterSpacing: '0.08em', textTransform: 'uppercase',
          flexWrap: 'wrap',
        }}>
          <span><span style={{ display: 'inline-block', width: 10, height: 10, background: 'var(--bone)', marginRight: 7, verticalAlign: 'middle' }} />Kept</span>
          <span><span style={{ display: 'inline-block', width: 10, height: 10, background: 'var(--signal)', marginRight: 7, verticalAlign: 'middle' }} />Identifier</span>
          <span><span style={{ display: 'inline-block', width: 10, height: 10, background: 'rgba(143,58,48,0.6)', marginRight: 7, verticalAlign: 'middle' }} />Dropped</span>
          <span><span style={{ display: 'inline-block', width: 10, height: 10, background: 'rgba(166,159,147,0.3)', marginRight: 7, verticalAlign: 'middle' }} />Filler</span>
        </div>
      </div>

      {/* Hover tooltip */}
      {hover && hover.tok && dropReason[hover.tok.k] && (
        <div style={{
          marginTop: 16, padding: '10px 14px', background: 'rgba(16,15,13,0.7)',
          border: '1px solid var(--line)', borderRadius: 3,
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
  background: 'transparent', border: '1px solid var(--line)', color: 'var(--stone-2)',
  padding: '6px 11px', fontFamily: 'JetBrains Mono, monospace', fontSize: 10.5,
  letterSpacing: '0.08em', cursor: 'pointer', borderRadius: 2, textTransform: 'uppercase',
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
