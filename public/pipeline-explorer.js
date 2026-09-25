const { useState: usePE, useEffect: useEfPE, useRef: useRfPE, useMemo: useMmPE } = React;
const { TASKS } = window.BrevitasPipelineData;
function TypingBlock({ tokens, reveal, phase }) {
  const shown = tokens.slice(0, reveal);
  const typing = phase === "typing" && reveal < tokens.length;
  return /* @__PURE__ */ React.createElement("div", { className: "bv-transcript", style: {
    fontFamily: "Newsreader, serif",
    fontSize: 15.5,
    lineHeight: 1.68,
    color: "var(--bone)",
    display: "grid"
  } }, /* @__PURE__ */ React.createElement("div", { "aria-hidden": "true", style: { gridArea: "1 / 1", visibility: "hidden", pointerEvents: "none" } }, tokens.map((tok, i) => /* @__PURE__ */ React.createElement(React.Fragment, { key: i }, tok.t, i < tokens.length - 1 ? " " : ""))), /* @__PURE__ */ React.createElement("div", { style: { gridArea: "1 / 1", alignSelf: "start" } }, shown.map((tok, i) => /* @__PURE__ */ React.createElement(React.Fragment, { key: i }, tok.t, i < shown.length - 1 ? " " : "")), typing && /* @__PURE__ */ React.createElement("span", { style: {
    display: "inline-block",
    width: 7,
    height: 15,
    background: "var(--bronze)",
    verticalAlign: "text-bottom",
    marginLeft: 2,
    animation: "bvCursorBlink 800ms steps(2) infinite"
  } })));
}
function InputRoute({ plan, phase }) {
  const active = phase === "routing";
  const isRead = plan.cached > 0;
  return /* @__PURE__ */ React.createElement("div", { style: {
    padding: "11px 12px",
    border: `1px solid ${active ? "var(--signal)" : "var(--line)"}`,
    background: active ? "rgba(141, 224, 207, 0.055)" : "var(--component-bg-dark)",
    borderRadius: 5,
    transition: "border-color 220ms, background 220ms"
  } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, marginBottom: 6, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 9.5, letterSpacing: "0.12em", color: "var(--signal)" } }, active ? "ROUTER CHECKING" : plan.label), /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 9.5, color: "var(--stone-2)", whiteSpace: "nowrap" } }, isRead ? `${plan.cached.toLocaleString()} cached \xB7 ${plan.fresh.toLocaleString()} fresh` : `${plan.total.toLocaleString()} sent in full`)), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Newsreader, serif", fontSize: 13, lineHeight: 1.35, color: "var(--stone-2)" } }, plan.detail));
}
function HopCard({ role, subtitle, tokens, reveal, phase, plan, outputCost }) {
  const active = phase === "routing" || phase === "typing";
  const pending = phase === "pending";
  return /* @__PURE__ */ React.createElement("div", { style: {
    flex: "1 1 0",
    minWidth: 0,
    borderTop: active ? "2px solid var(--bronze)" : "2px solid transparent",
    background: active ? "var(--graphite)" : "transparent",
    padding: "18px 22px 22px",
    transition: "border-color 300ms, background 300ms, opacity 300ms",
    opacity: pending ? 0.4 : 1,
    position: "relative",
    display: "flex",
    flexDirection: "column",
    gap: 12
  } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("div", { style: { minWidth: 0, flex: "1 1 220px" } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Inter Tight, system-ui, sans-serif", fontSize: 14.5, fontWeight: 500, color: "var(--bronze)", letterSpacing: "0.035em", marginBottom: 5 } }, role), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Inter Tight, system-ui, sans-serif", fontSize: 23, fontWeight: 500, lineHeight: 1.15, color: "var(--bone)", letterSpacing: "-0.02em", whiteSpace: "nowrap" } }, subtitle)), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 11, color: "var(--stone-2)", flex: "0 0 auto", whiteSpace: "nowrap", paddingTop: 2 } }, "in ", plan.total.toLocaleString(), " \xB7 out ", outputCost)), /* @__PURE__ */ React.createElement(InputRoute, { plan, phase }), /* @__PURE__ */ React.createElement(TypingBlock, { tokens, reveal, phase }));
}
function MobilePipelineSlides({ task, phases, reveals, slideIndex, setSlideIndex, onPrimary, onReplay }) {
  var _a;
  const hops = [
    { number: "01", role: "Architect", subtitle: "Chooses the approach", tokens: task.a1, output: task.a1Tokens, plan: task.cachePlan[0] },
    { number: "02", role: "Builder", subtitle: "Writes the implementation", tokens: task.a2, output: task.a2Tokens, plan: task.cachePlan[1] },
    { number: "03", role: "Reviewer", subtitle: "Flags risks and approves", tokens: task.a3, output: task.a3Tokens, plan: task.cachePlan[2] }
  ];
  const stateLabel = (phase2) => {
    if (phase2 === "done") return "Complete";
    if (phase2 === "pending") return "Waiting";
    if (phase2 === "routing") return "Routing";
    if (phase2 === "typing") return "Writing";
    return "Working";
  };
  const hop = hops[slideIndex];
  const phase = phases[slideIndex];
  const nextRole = (_a = hops[slideIndex + 1]) == null ? void 0 : _a.role;
  const primaryLabel = phase !== "done" ? "Finish this hop" : nextRole ? `Next: ${nextRole}` : "Replay demo";
  return /* @__PURE__ */ React.createElement("section", { className: "bv-mobile-slides", "aria-label": "Agent pipeline slides" }, /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-slide" }, /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-slide-topline" }, /* @__PURE__ */ React.createElement("span", null, "Agent ", hop.number, ": ", hop.role), /* @__PURE__ */ React.createElement("span", { className: `bv-mobile-slide-state is-${phase}`, "aria-live": "polite" }, stateLabel(phase))), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-slide-heading" }, /* @__PURE__ */ React.createElement("h3", null, hop.subtitle), /* @__PURE__ */ React.createElement("span", null, "One agent at a time")), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-token-stats", "aria-label": "Input cache usage" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Full input"), /* @__PURE__ */ React.createElement("strong", null, hop.plan.total.toLocaleString())), /* @__PURE__ */ React.createElement("span", { className: "bv-mobile-token-arrow", "aria-hidden": "true" }, "\u2192"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Cached"), /* @__PURE__ */ React.createElement("strong", null, hop.plan.cached.toLocaleString())), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-token-saved" }, /* @__PURE__ */ React.createElement("span", null, "Fresh input"), /* @__PURE__ */ React.createElement("strong", null, hop.plan.fresh.toLocaleString()))), /* @__PURE__ */ React.createElement(InputRoute, { plan: hop.plan, phase }), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-transcript" }, /* @__PURE__ */ React.createElement(
    TypingBlock,
    {
      tokens: hop.tokens,
      reveal: reveals[slideIndex],
      phase
    }
  )), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-slide-nav", "aria-label": "Choose an agent slide" }, hops.map((item, index) => /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      key: item.number,
      className: index === slideIndex ? "is-active" : "",
      "aria-current": index === slideIndex ? "step" : void 0,
      "aria-label": `Show ${item.role} slide`,
      disabled: phases[index] === "pending" && index > slideIndex,
      onClick: () => setSlideIndex(index)
    },
    /* @__PURE__ */ React.createElement("span", null, item.number),
    item.role
  ))), /* @__PURE__ */ React.createElement("div", { className: "bv-mobile-slide-actions" }, /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "bv-mobile-back",
      disabled: slideIndex === 0,
      onClick: () => setSlideIndex((index) => Math.max(0, index - 1))
    },
    "\u2190 Back"
  ), /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "bv-mobile-next",
      disabled: phase === "pending",
      onClick: phase === "done" && !nextRole ? onReplay : onPrimary
    },
    primaryLabel,
    " \u2192"
  ))));
}
function CostReadout({ task, progress }) {
  const baselineCum = useMmPE(() => {
    if (progress <= 0) return 0;
    if (progress === 1) return task.baseline.call1;
    if (progress === 2) return task.baseline.call1 + task.baseline.call2;
    return task.baseline.total;
  }, [task, progress]);
  const actualCum = useMmPE(() => task.cachePlan.slice(0, progress).reduce((sum, hop) => sum + hop.cost, 0), [task, progress]);
  const cachedCum = useMmPE(() => task.cachePlan.slice(0, progress).reduce((sum, hop) => sum + hop.cached, 0), [task, progress]);
  const withPct = baselineCum ? Math.round(actualCum / baselineCum * 100) : 100;
  const pct = 100 - withPct;
  const saving = pct >= 0;
  const Cell = ({ label, value, color, bold }) => /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 4, minWidth: 0 } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 9.5, letterSpacing: "0.14em", textTransform: "uppercase", color: "var(--stone-2)" } }, label), /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: bold ? 18 : 14, color: color || "var(--bone)", fontVariantNumeric: "tabular-nums" } }, value));
  return /* @__PURE__ */ React.createElement("div", { className: "bv-cost-readout", style: {
    background: "var(--graphite)",
    padding: "15px 20px 14px",
    marginTop: 12,
    display: "grid",
    gridTemplateColumns: "auto 1fr auto auto auto auto",
    gap: 28,
    alignItems: "center"
  } }, /* @__PURE__ */ React.createElement("div", { className: "bv-cost-heading", style: {
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 10.5,
    letterSpacing: "0.14em",
    textTransform: "uppercase",
    color: "var(--bronze)"
  } }, "input billing", /* @__PURE__ */ React.createElement("div", { style: { color: "var(--stone)", fontSize: 8.5, letterSpacing: "0.06em", marginTop: 3 } }, "full context stays intact")), /* @__PURE__ */ React.createElement("div", { className: "bv-cost-progress", style: { display: "flex", flexDirection: "column", gap: 6 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "space-between", fontFamily: "JetBrains Mono, monospace", fontSize: 9, color: "var(--stone)", letterSpacing: "0.12em" } }, /* @__PURE__ */ React.createElement("span", null, "hop 1"), /* @__PURE__ */ React.createElement("span", null, "hop 2"), /* @__PURE__ */ React.createElement("span", null, "hop 3")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, height: 10 } }, [1, 2, 3].map((n) => {
    const b = task.baseline["call" + n];
    const o = task.cachePlan[n - 1].cost;
    const pctBar = Math.min(100, Math.round(o / b * 100));
    const reached = progress >= n;
    return /* @__PURE__ */ React.createElement("div", { key: n, style: { flex: 1, background: "rgba(166,159,147,0.15)", position: "relative", overflow: "hidden" } }, /* @__PURE__ */ React.createElement("div", { style: {
      position: "absolute",
      inset: 0,
      width: reached ? pctBar + "%" : 0,
      background: o > b ? "var(--bronze)" : "var(--signal)",
      transition: "width 900ms cubic-bezier(.4,0,.2,1)"
    } }));
  }))), /* @__PURE__ */ React.createElement(Cell, { label: "Context sent", value: baselineCum.toLocaleString(), color: "var(--bone)" }), /* @__PURE__ */ React.createElement(Cell, { label: "Read from cache", value: cachedCum.toLocaleString(), color: "var(--signal)" }), /* @__PURE__ */ React.createElement(Cell, { label: "Relative input cost", value: progress ? `${withPct}%` : "\u2014", color: saving ? "var(--bone)" : "var(--bronze)" }), /* @__PURE__ */ React.createElement("div", { className: "bv-cost-percent", style: { display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2 } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Newsreader, serif", fontSize: 40, lineHeight: 1, color: saving ? "var(--signal)" : "var(--bronze)", letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums" } }, progress ? Math.abs(pct) : 0, /* @__PURE__ */ React.createElement("span", { style: { fontSize: 22, color: "var(--stone-2)" } }, "%")), /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 9, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--stone-2)" } }, saving ? "lower input cost" : "cache warm-up")));
}
function PipelineFieldBg() {
  return /* @__PURE__ */ React.createElement("div", { className: "bv-field-wrap", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("div", { className: "bv-field-grid" }), /* @__PURE__ */ React.createElement("div", { className: "bv-field-glow" }), /* @__PURE__ */ React.createElement("svg", { style: { position: "absolute", inset: 0, width: "100%", height: "100%" } }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("linearGradient", { id: "bvRail", x1: "0", x2: "1" }, /* @__PURE__ */ React.createElement("stop", { offset: "0", stopColor: "rgba(166,159,147,0)" }), /* @__PURE__ */ React.createElement("stop", { offset: "0.15", stopColor: "rgba(166,159,147,0.22)" }), /* @__PURE__ */ React.createElement("stop", { offset: "0.85", stopColor: "rgba(166,159,147,0.22)" }), /* @__PURE__ */ React.createElement("stop", { offset: "1", stopColor: "rgba(166,159,147,0)" }))), /* @__PURE__ */ React.createElement(
    "path",
    {
      d: "M 8% 40% C 30% 40%, 35% 52%, 50% 52% S 70% 64%, 92% 64%",
      stroke: "url(#bvRail)",
      strokeWidth: "1",
      fill: "none",
      strokeDasharray: "2 6",
      opacity: "0.5"
    }
  ), /* @__PURE__ */ React.createElement(
    "path",
    {
      d: "M 8% 72% C 30% 72%, 35% 58%, 50% 58% S 70% 44%, 92% 44%",
      stroke: "url(#bvRail)",
      strokeWidth: "1",
      fill: "none",
      strokeDasharray: "2 6",
      opacity: "0.35"
    }
  )), [0, 1, 2, 3].map((i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "bv-field-packet", style: {
    offsetPath: 'path("M 0 120 C 300 120, 400 180, 600 180 S 900 260, 1200 260")',
    WebkitOffsetPath: 'path("M 0 120 C 300 120, 400 180, 600 180 S 900 260, 1200 260")',
    animationDelay: i * 2.2 + "s",
    animationDuration: 7 + i * 1.5 + "s",
    opacity: 0
  } })));
}
function PipelineExplorer() {
  const [activeId, setActiveId] = usePE("rate-limiter");
  const [activeHop, setActiveHop] = usePE(0);
  const [reveal, setReveal] = usePE(0);
  const [phases, setPhases] = usePE(["pending", "pending", "pending"]);
  const [runToken, setRunToken] = usePE(0);
  const [startHop, setStartHop] = usePE(0);
  const [isVisible, setIsVisible] = usePE(false);
  const [mobileSlide, setMobileSlide] = usePE(0);
  const skipRef = useRfPE({ finishCurrent: () => {
  }, advanceHop: () => {
  } });
  const rootRef = useRfPE(null);
  const task = useMmPE(() => TASKS.find((t) => t.id === activeId), [activeId]);
  useEfPE(() => {
    let cancelled = false;
    let skipCurrent = false;
    let advanceNow = false;
    const timers = [];
    const push = (fn, ms) => {
      const id = setTimeout(() => {
        if (!cancelled) fn();
      }, ms);
      timers.push(id);
    };
    const wait = (ms) => new Promise((r) => push(r, ms));
    skipRef.current = {
      finishCurrent: () => {
        skipCurrent = true;
      },
      advanceHop: () => {
        advanceNow = true;
        skipCurrent = true;
      }
    };
    async function runHop(h) {
      if (cancelled) return;
      skipCurrent = false;
      advanceNow = false;
      const hopTokens = [task.a1, task.a2, task.a3][h];
      setActiveHop(h);
      setReveal(0);
      setPhases((p) => {
        const n = [...p];
        n[h] = "routing";
        return n;
      });
      await wait(skipCurrent ? 80 : 850);
      if (cancelled) return;
      setPhases((p) => {
        const n = [...p];
        n[h] = "typing";
        return n;
      });
      const total = hopTokens.length;
      const perTok = Math.max(30, Math.min(70, 5600 / total));
      for (let i = 0; i <= total; i++) {
        if (cancelled) return;
        if (skipCurrent) {
          setReveal(total);
          break;
        }
        setReveal(i);
        await wait(perTok);
      }
      if (cancelled) return;
      setPhases((p) => {
        const n = [...p];
        n[h] = "done";
        return n;
      });
      await wait(advanceNow ? 0 : skipCurrent ? 150 : 650);
    }
    (async () => {
      const initPhases = ["pending", "pending", "pending"];
      const hopTokArr = [task.a1, task.a2, task.a3];
      for (let i = 0; i < startHop; i++) {
        initPhases[i] = "done";
      }
      setPhases(initPhases);
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
      timers.forEach((id) => clearTimeout(id));
    };
  }, [activeId, runToken, task]);
  const reveals = useMmPE(() => {
    const lens = [task.a1.length, task.a2.length, task.a3.length];
    return [0, 1, 2].map((h) => {
      if (phases[h] === "pending") return 0;
      if (phases[h] === "routing") return 0;
      if (phases[h] === "typing") return h === activeHop ? reveal : 0;
      return lens[h];
    });
  }, [phases, activeHop, reveal, task]);
  const costProgress = useMmPE(() => {
    let p = 0;
    for (let h = 0; h < 3; h++) {
      if (phases[h] !== "pending") p = h + 1;
    }
    return p;
  }, [phases]);
  const stripRef = useRfPE(null);
  useEfPE(() => {
    const container = stripRef.current;
    const el = container == null ? void 0 : container.children[activeHop];
    if (!container || !el) return;
    const targetLeft = el.offsetLeft - (container.clientWidth - el.offsetWidth) / 2;
    container.scrollTo({ left: Math.max(0, targetLeft), behavior: "smooth" });
  }, [activeHop]);
  function pickTask(id) {
    setActiveId(id);
    setMobileSlide(0);
    setStartHop(0);
    setRunToken((n) => n + 1);
  }
  function replay() {
    setMobileSlide(0);
    setStartHop(0);
    setRunToken((n) => n + 1);
  }
  function skipToEnd() {
    setMobileSlide(2);
    setStartHop(3);
    setPhases(["done", "done", "done"]);
    setRunToken((n) => n + 1e4);
  }
  function advanceMobileSlide() {
    if (phases[mobileSlide] !== "done") {
      skipRef.current.finishCurrent();
      return;
    }
    const nextSlide = Math.min(2, mobileSlide + 1);
    setMobileSlide(nextSlide);
    setStartHop(nextSlide);
    setRunToken((n) => n + 1);
  }
  useEfPE(() => {
    if (!rootRef.current) return;
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((e) => setIsVisible(e.isIntersecting && e.intersectionRatio > 0.3));
    }, { threshold: [0, 0.3, 0.6, 1] });
    obs.observe(rootRef.current);
    return () => obs.disconnect();
  }, []);
  useEfPE(() => {
    if (!isVisible) return;
    function onKey(ev) {
      const t = ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (ev.key === "ArrowRight") {
        ev.preventDefault();
        const allDone = phases.every((p) => p === "done");
        if (allDone) return;
        const curPhase = phases[activeHop];
        if (curPhase === "routing" || curPhase === "typing") {
          skipRef.current.finishCurrent();
        } else {
          skipRef.current.advanceHop();
        }
      } else if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        const back = Math.max(0, activeHop - 1);
        setStartHop(back);
        setRunToken((n) => n + 1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isVisible, phases, activeHop]);
  return /* @__PURE__ */ React.createElement("div", { ref: rootRef, className: "bv-explorer", style: { width: "100%", position: "relative" } }, /* @__PURE__ */ React.createElement(PipelineFieldBg, null), /* @__PURE__ */ React.createElement("style", null, `
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
          .bv-mobile-slide-heading > span,
          .bv-mobile-token-stats span,
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
            font-family: 'Inter Tight', system-ui, sans-serif;
            font-size: 13px;
            font-weight: 500;
            letter-spacing: 0.035em;
          }
          .bv-mobile-slide-state { color: var(--stone-2); }
          .bv-mobile-slide-state.is-routing,
          .bv-mobile-slide-state.is-typing,
          .bv-mobile-slide-state.is-working { color: var(--signal); }
          .bv-mobile-slide-state.is-done { color: var(--bone); }
          .bv-mobile-slide-heading { margin: 18px 0; }
          .bv-mobile-slide-heading h3 {
            margin: 0 0 5px;
            color: var(--bone);
            font-family: 'Inter Tight', system-ui, sans-serif;
            font-size: clamp(27px, 8vw, 36px);
            font-weight: 500;
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
        /* Ambient glow removed \u2014 no decorative gradients behind the tool */
        .bv-field-glow { display: none !important; }
        @media (prefers-reduced-motion: reduce) {
          .bv-field-grid, .bv-field-packet { animation: none !important; }
          .bv-transcript span { animation-duration: 1ms !important; }
        }
      `), /* @__PURE__ */ React.createElement("div", { className: "bv-task-tabs", role: "group", "aria-label": "Choose a demo task", style: { display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap", marginBottom: 14 } }, TASKS.map((t) => {
    const active = t.id === activeId;
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: t.id,
        className: "bv-task-pill",
        "aria-pressed": active,
        onClick: () => pickTask(t.id),
        style: {
          background: active ? "var(--component-bg-dark-light)" : "transparent",
          color: active ? "var(--bone)" : "var(--stone-2)",
          border: "none",
          boxShadow: active ? "inset 0 -1.5px 0 var(--bronze)" : "none",
          padding: "9px 14px",
          fontFamily: "JetBrains Mono, monospace",
          fontSize: 12.5,
          letterSpacing: "0.025em",
          borderRadius: 5,
          cursor: "pointer",
          fontWeight: active ? 600 : 400
        }
      },
      t.label
    );
  })), /* @__PURE__ */ React.createElement("div", { className: "bv-prompt", style: {
    background: "var(--component-bg-dark-light)",
    padding: "16px 18px",
    borderRadius: 7,
    marginBottom: 14,
    display: "flex",
    alignItems: "center",
    gap: 16
  } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Newsreader, serif", fontSize: 19, lineHeight: 1.4, color: "var(--bone)", flex: 1, letterSpacing: "-0.005em" } }, task.user), /* @__PURE__ */ React.createElement("div", { className: "bv-prompt-actions", style: { display: "flex", gap: 8, flex: "0 0 auto", alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { className: "bv-keyboard-hint", "aria-label": "Use the left and right arrow keys to step through the animation" }, /* @__PURE__ */ React.createElement("kbd", null, "\u2190"), /* @__PURE__ */ React.createElement("kbd", null, "\u2192"), /* @__PURE__ */ React.createElement("span", null, "step")), /* @__PURE__ */ React.createElement("button", { onClick: replay, className: "bv-ctl-btn", style: btnStyle, "aria-label": "Replay the animation" }, "\u21BB Replay"), /* @__PURE__ */ React.createElement("button", { onClick: skipToEnd, className: "bv-ctl-btn", style: btnStyle, "aria-label": "Skip the animation and show the final result" }, "Skip to result \u2192"))), /* @__PURE__ */ React.createElement("div", { style: { position: "relative" }, className: "bv-pipe-grid" }, /* @__PURE__ */ React.createElement("div", { ref: stripRef, className: "bv-strip", style: {
    display: "flex",
    gap: 0,
    minWidth: 0,
    alignItems: "stretch"
  } }, [
    { role: "Agent 01: Architect", subtitle: "Chooses the approach", toks: task.a1, out: task.a1Tokens, plan: task.cachePlan[0] },
    { role: "Agent 02: Builder", subtitle: "Writes the implementation", toks: task.a2, out: task.a2Tokens, plan: task.cachePlan[1] },
    { role: "Agent 03: Reviewer", subtitle: "Flags risks \xB7 approves", toks: task.a3, out: task.a3Tokens, plan: task.cachePlan[2] }
  ].map((h, i) => /* @__PURE__ */ React.createElement(
    HopCard,
    {
      key: i,
      role: h.role,
      subtitle: h.subtitle,
      tokens: h.toks,
      reveal: reveals[i],
      phase: phases[i],
      plan: h.plan,
      outputCost: h.out.toLocaleString()
    }
  )))), /* @__PURE__ */ React.createElement(
    MobilePipelineSlides,
    {
      task,
      phases,
      reveals,
      slideIndex: mobileSlide,
      setSlideIndex: setMobileSlide,
      onPrimary: advanceMobileSlide,
      onReplay: replay
    }
  ), /* @__PURE__ */ React.createElement(CostReadout, { task, progress: costProgress }));
}
const btnStyle = {
  background: "var(--graphite)",
  border: "1px solid var(--stone)",
  color: "var(--bone)",
  padding: "8px 15px",
  fontFamily: "JetBrains Mono, monospace",
  fontSize: 11.5,
  letterSpacing: "0.08em",
  cursor: "pointer",
  borderRadius: 5,
  textTransform: "uppercase",
  fontWeight: 500
};
const kbdStyle = {
  display: "inline-block",
  padding: "2px 6px",
  border: "1px solid currentColor",
  borderRadius: 2,
  fontFamily: "JetBrains Mono, monospace",
  fontSize: 10,
  lineHeight: 1,
  marginRight: 2
};
Object.assign(window, { PipelineExplorer });
