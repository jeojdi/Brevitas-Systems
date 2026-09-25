const { useState: useSTH, useEffect: useEfSTH, useRef: useRfSTH, useMemo: useMmSTH } = React;
const TECHNIQUES = [
  {
    n: "01",
    title: "Provider-native prompt caching",
    short: "Cache",
    body: "Repeated system instructions, tools, and conversation history stay intact and are billed at the provider\u2019s cache rate. Brevitas places Anthropic breakpoints automatically; OpenAI and DeepSeek cache stable prefixes natively.",
    tag: "Lossless",
    demo: "/product"
  },
  {
    n: "02",
    title: "Byte-stable prefix preservation",
    short: "Stabilize",
    body: "Reusable context stays in the same order and the newest message stays at the end. Brevitas does not rewrite the stable prefix, protecting cache hits that ordinary prompt mutation would destroy.",
    tag: "Byte-stable"
  },
  {
    n: "03",
    title: "Shared-prefix layout across agents",
    short: "Share",
    body: "When context is proven identical across agents, Brevitas can move that shared block into a common leading position. It only does this when the new layout increases the cacheable prefix; the content itself is unchanged.",
    tag: "Multi-agent"
  },
  {
    n: "04",
    title: "Accuracy-first context retrieval",
    short: "Retrieve",
    body: "For opted-in workloads with long histories, dense and lexical retrieval select relevant context while preserving conversation structure. Low confidence, an empty index, or an oversized session falls back to full context.",
    tag: "Opt-in"
  },
  {
    n: "05",
    title: "Provider-reported savings measurement",
    short: "Measure",
    body: "Brevitas reads the provider\u2019s real cache-hit, cache-write, input, and output fields. The router learns from observed results, and savings are reported from actual billing behavior rather than guessed token deletion.",
    tag: "Receipts"
  },
  {
    n: "06",
    title: "Cost-aware, do-no-harm routing",
    short: "Route",
    body: "Every call is priced across cache-only, retrieval, and untouched pass-through paths. If an optimization lacks evidence, confidence, or real savings, Brevitas sends the original request unchanged.",
    tag: "Per-call"
  }
];
const LAYOUT = [
  { ang: -150 },
  //  1 — upper-left
  { ang: -90 },
  //  2 — top
  { ang: -30 },
  //  3 — upper-right
  { ang: 30 },
  //  4 — lower-right
  { ang: 90 },
  //  5 — bottom
  { ang: 150 }
  //  6 — lower-left
];
const HUB = {
  w: 1040,
  h: 620,
  cx: 520,
  cy: 310,
  rx: 340,
  // horizontal radius (leaves room for 220px-wide cards at each end)
  ry: 210
  // vertical radius
};
function polarToXY(angDeg) {
  const a = angDeg * Math.PI / 180;
  return {
    x: HUB.cx + HUB.rx * Math.cos(a),
    y: HUB.cy + HUB.ry * Math.sin(a)
  };
}
function wavyPath(x1, y1, x2, y2, amp = 0, phase = 0) {
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const dx = x2 - x1, dy = y2 - y1;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const offset = amp * Math.sin(phase);
  const cx = mx + nx * offset;
  const cy = my + ny * offset;
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${x2.toFixed(1)} ${y2.toFixed(1)}`;
}
function HubNodeCard({ tech, pos, isActive, isDim, onEnter, onLeave, onClick }) {
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      onMouseEnter: onEnter,
      onMouseLeave: onLeave,
      onFocus: onEnter,
      onBlur: onLeave,
      onClick,
      "data-sth-node-index": tech.idx,
      style: {
        position: "absolute",
        left: pos.x / HUB.w * 100 + "%",
        top: pos.y / HUB.h * 100 + "%",
        transform: `translate(-50%, -50%) ${isActive ? "scale(1.04)" : "scale(1)"}`,
        width: "min(220px, 26vw)",
        minWidth: 180,
        padding: "14px 16px",
        background: isActive ? "var(--graphite)" : "var(--component-bg-dark)",
        border: "1px solid " + (isActive ? "var(--bronze)" : "var(--line)"),
        boxShadow: isActive ? "0 0 0 3px rgba(138,98,66,0.18), 0 10px 30px rgba(0,0,0,0.4)" : "0 4px 14px rgba(0,0,0,0.25)",
        opacity: isDim ? 0.38 : 1,
        borderRadius: 4,
        textAlign: "left",
        cursor: "pointer",
        transition: "transform 260ms cubic-bezier(.4,0,.2,1), border-color 260ms, box-shadow 260ms, opacity 260ms, background 260ms",
        zIndex: isActive ? 3 : 2,
        color: "var(--bone)",
        fontFamily: "inherit"
      }
    },
    /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "baseline", gap: 10, marginBottom: 6 } }, /* @__PURE__ */ React.createElement("span", { style: {
      fontFamily: "JetBrains Mono, monospace",
      fontSize: 10,
      letterSpacing: "0.14em",
      color: isActive ? "var(--bronze)" : "var(--stone)",
      transition: "color 260ms"
    } }, tech.n), /* @__PURE__ */ React.createElement("span", { style: {
      fontFamily: "JetBrains Mono, monospace",
      fontSize: 9,
      letterSpacing: "0.1em",
      color: "var(--stone-2)",
      textTransform: "uppercase",
      marginLeft: "auto"
    } }, tech.tag)),
    /* @__PURE__ */ React.createElement("div", { style: {
      fontFamily: "Newsreader, serif",
      fontSize: 17,
      letterSpacing: "-0.01em",
      lineHeight: 1.2,
      color: "var(--bone)"
    } }, tech.short),
    /* @__PURE__ */ React.createElement("div", { style: {
      fontFamily: "JetBrains Mono, monospace",
      fontSize: 10,
      color: "var(--stone-2)",
      marginTop: 6,
      letterSpacing: "0.04em",
      lineHeight: 1.4,
      display: "-webkit-box",
      WebkitLineClamp: 2,
      WebkitBoxOrient: "vertical",
      overflow: "hidden"
    } }, tech.title)
  );
}
function HubCenter({ isHovered }) {
  return /* @__PURE__ */ React.createElement("div", { style: {
    position: "absolute",
    left: HUB.cx / HUB.w * 100 + "%",
    top: HUB.cy / HUB.h * 100 + "%",
    transform: "translate(-50%, -50%)",
    width: "min(180px, 22vw)",
    minWidth: 150,
    padding: "18px 20px",
    background: "var(--graphite)",
    border: "1px solid var(--bronze)",
    borderRadius: 4,
    boxShadow: "0 0 0 3px rgba(138,98,66,0.18), 0 12px 36px rgba(0,0,0,0.5)",
    textAlign: "center",
    zIndex: 4,
    pointerEvents: "none"
  } }, /* @__PURE__ */ React.createElement("div", { style: {
    fontFamily: "Newsreader, serif",
    fontSize: 22,
    letterSpacing: "-0.015em",
    lineHeight: 1.15,
    color: "var(--bone)",
    marginBottom: 8
  } }, /* @__PURE__ */ React.createElement("em", { style: { fontStyle: "italic" } }, "Cost-aware routing")), /* @__PURE__ */ React.createElement("div", { style: {
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 9.5,
    letterSpacing: "0.08em",
    color: "var(--stone-2)"
  } }, "cache \xB7 retrieve \xB7", /* @__PURE__ */ React.createElement("br", null), "pass through"));
}
function SixTechniquesHub() {
  const [hoverIdx, setHoverIdx] = useSTH(null);
  const [selectedIdx, setSelectedIdx] = useSTH(0);
  const [phase, setPhase] = useSTH(0);
  const [isVisible, setIsVisible] = useSTH(false);
  const rootRef = useRfSTH(null);
  const stripRef = useRfSTH(null);
  const positions = useMmSTH(
    () => LAYOUT.map((lay, i) => ({ ...polarToXY(lay.ang), ang: lay.ang, idx: i })),
    []
  );
  useEfSTH(() => {
    if (hoverIdx == null) return;
    let raf, start = performance.now();
    function tick(t) {
      setPhase((t - start) / 520);
      raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [hoverIdx]);
  useEfSTH(() => {
    if (!rootRef.current) return;
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((e) => setIsVisible(e.isIntersecting && e.intersectionRatio > 0.25));
    }, { threshold: [0, 0.25, 0.5] });
    obs.observe(rootRef.current);
    return () => obs.disconnect();
  }, []);
  useEfSTH(() => {
    if (!isVisible) return;
    function onKey(ev) {
      const t = ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (ev.key === "ArrowRight") {
        ev.preventDefault();
        setSelectedIdx((i) => (i + 1) % TECHNIQUES.length);
      } else if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        setSelectedIdx((i) => (i - 1 + TECHNIQUES.length) % TECHNIQUES.length);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isVisible]);
  useEfSTH(() => {
    const strip = stripRef.current;
    const child = strip == null ? void 0 : strip.children[selectedIdx];
    if (!strip || !child) return;
    const targetLeft = child.offsetLeft - (strip.clientWidth - child.offsetWidth) / 2;
    strip.scrollTo({ left: Math.max(0, targetLeft), behavior: "smooth" });
  }, [selectedIdx]);
  const activeIdx = hoverIdx != null ? hoverIdx : selectedIdx;
  const active = TECHNIQUES[activeIdx];
  return /* @__PURE__ */ React.createElement("div", { ref: rootRef }, /* @__PURE__ */ React.createElement("style", null, `
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
        @media (max-width: 720px) {
          .sth-detail-grid { grid-template-columns: 1fr !important; gap: 16px !important; }
          .sth-detail-nav { display: none !important; }
        }
      `), /* @__PURE__ */ React.createElement("div", { className: "sth-hub-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "sth-hub-stage" }, /* @__PURE__ */ React.createElement("svg", { viewBox: `0 0 ${HUB.w} ${HUB.h}`, preserveAspectRatio: "xMidYMid meet", style: { overflow: "visible" } }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("linearGradient", { id: "sthLineActive", x1: "0", y1: "0", x2: "1", y2: "0" }, /* @__PURE__ */ React.createElement("stop", { offset: "0%", stopColor: "var(--bronze)", stopOpacity: "0.1" }), /* @__PURE__ */ React.createElement("stop", { offset: "50%", stopColor: "var(--bronze)", stopOpacity: "0.95" }), /* @__PURE__ */ React.createElement("stop", { offset: "100%", stopColor: "var(--signal)", stopOpacity: "0.85" })), /* @__PURE__ */ React.createElement("radialGradient", { id: "sthHubRing", cx: "50%", cy: "50%", r: "50%" }, /* @__PURE__ */ React.createElement("stop", { offset: "0%", stopColor: "var(--bronze)", stopOpacity: "0.0" }), /* @__PURE__ */ React.createElement("stop", { offset: "80%", stopColor: "var(--bronze)", stopOpacity: "0.0" }), /* @__PURE__ */ React.createElement("stop", { offset: "100%", stopColor: "var(--bronze)", stopOpacity: "0.22" }))), /* @__PURE__ */ React.createElement(
    "ellipse",
    {
      cx: HUB.cx,
      cy: HUB.cy,
      rx: HUB.rx,
      ry: HUB.ry,
      fill: "none",
      stroke: "var(--line)",
      strokeDasharray: "2 7",
      strokeWidth: "1",
      opacity: "0.55"
    }
  ), positions.map((p, i) => {
    const isActive = i === activeIdx;
    const isHoveredExactly = hoverIdx === i;
    const amp = isHoveredExactly ? 28 : 0;
    const d = wavyPath(HUB.cx, HUB.cy, p.x, p.y, amp, phase + i * 0.6);
    return /* @__PURE__ */ React.createElement("g", { key: i }, /* @__PURE__ */ React.createElement(
      "path",
      {
        d: `M ${HUB.cx} ${HUB.cy} L ${p.x} ${p.y}`,
        stroke: "var(--line)",
        strokeWidth: "1",
        fill: "none",
        opacity: isActive ? 0 : 0.85,
        style: { transition: "opacity 300ms" }
      }
    ), /* @__PURE__ */ React.createElement(
      "path",
      {
        d,
        stroke: isHoveredExactly ? "url(#sthLineActive)" : "var(--bronze)",
        strokeWidth: isHoveredExactly ? 2 : 1.4,
        fill: "none",
        opacity: isActive ? 1 : 0,
        strokeLinecap: "round",
        style: { transition: "opacity 260ms, stroke-width 260ms" }
      }
    ), /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: p.x,
        cy: p.y,
        r: isActive ? 4 : 2.5,
        fill: isActive ? "var(--bronze)" : "var(--stone)",
        opacity: isActive ? 0.9 : 0.6,
        style: { transition: "r 260ms, opacity 260ms" }
      }
    ));
  }), /* @__PURE__ */ React.createElement("circle", { cx: HUB.cx, cy: HUB.cy, r: "72", fill: "url(#sthHubRing)" })), /* @__PURE__ */ React.createElement("div", { className: "sth-layer", style: { pointerEvents: "none" } }, /* @__PURE__ */ React.createElement(HubCenter, null), /* @__PURE__ */ React.createElement("div", { style: { pointerEvents: "auto", position: "absolute", inset: 0 } }, TECHNIQUES.map((t, i) => /* @__PURE__ */ React.createElement(
    HubNodeCard,
    {
      key: i,
      tech: { ...t, idx: i },
      pos: positions[i],
      isActive: i === activeIdx,
      isDim: hoverIdx != null && hoverIdx !== i,
      onEnter: () => setHoverIdx(i),
      onLeave: () => setHoverIdx(null),
      onClick: () => setSelectedIdx(i)
    }
  ))))), /* @__PURE__ */ React.createElement(
    "div",
    {
      ref: stripRef,
      className: "sth-hub-strip",
      style: {
        gap: 14,
        overflowX: "auto",
        scrollSnapType: "x proximity",
        paddingBottom: 14,
        WebkitOverflowScrolling: "touch"
      }
    },
    TECHNIQUES.map((t, i) => {
      const isActive = i === activeIdx;
      return /* @__PURE__ */ React.createElement(
        "button",
        {
          key: i,
          onClick: () => setSelectedIdx(i),
          style: {
            flex: "0 0 80%",
            scrollSnapAlign: "center",
            padding: "18px 18px",
            border: "1px solid " + (isActive ? "var(--bronze)" : "var(--line)"),
            background: isActive ? "var(--graphite)" : "transparent",
            color: "var(--bone)",
            borderRadius: 4,
            textAlign: "left",
            cursor: "pointer",
            minWidth: 240
          }
        },
        /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 10, letterSpacing: "0.14em", color: "var(--bronze)", marginBottom: 6 } }, t.n, " \xB7 ", t.tag),
        /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "Newsreader, serif", fontSize: 18, letterSpacing: "-0.01em", marginBottom: 6 } }, t.short),
        /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "JetBrains Mono, monospace", fontSize: 10, color: "var(--stone-2)", lineHeight: 1.45 } }, t.title)
      );
    })
  )), /* @__PURE__ */ React.createElement("div", { className: "sth-detail-grid", style: {
    marginTop: 22,
    border: "1px solid var(--line)",
    background: "var(--component-bg-dark-light)",
    padding: "26px 28px",
    borderRadius: 4,
    display: "grid",
    gridTemplateColumns: "120px 1fr",
    gap: 28,
    alignItems: "start"
  } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: {
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 38,
    fontWeight: 300,
    color: "var(--bronze)",
    letterSpacing: "-0.02em"
  } }, active.n), /* @__PURE__ */ React.createElement("div", { style: {
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 10,
    letterSpacing: "0.14em",
    color: "var(--stone-2)",
    textTransform: "uppercase",
    marginTop: 4
  } }, active.tag)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: {
    fontFamily: "Newsreader, serif",
    fontSize: 24,
    letterSpacing: "-0.015em",
    color: "var(--bone)",
    marginBottom: 10,
    lineHeight: 1.2
  } }, active.title), /* @__PURE__ */ React.createElement("p", { style: {
    fontFamily: "Newsreader, serif",
    fontSize: 15,
    lineHeight: 1.65,
    color: "var(--stone-2)",
    margin: 0,
    maxWidth: 680
  } }, active.body))));
}
Object.assign(window, { SixTechniquesHub });
