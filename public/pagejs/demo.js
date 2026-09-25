(function(){
const MODELS = [
  { key: "anthropic", name: "Claude Opus 4", badge: "Anthropic", in: 15, out: 75, cached: 1.5 },
  { key: "openai", name: "GPT-5.6", badge: "OpenAI", in: 2.5, out: 10, cached: 0.25 },
  { key: "deepseek", name: "DeepSeek-V4", badge: "DeepSeek", in: 0.28, out: 0.42, cached: 56e-4 }
];
const PREFIX_TOKENS = 1600;
const SEMANTIC_THRESHOLD = 0.68;
const TRAVEL = 640;
const DWELL = 540;
const EXPRESS = 1e3;
const STOP = new Set("a an the of to in on for and or but with without as at by from into over under is are was were be been being do does did done have has had will would shall should can could may might must i you he she it we they me him her us them my your his its our their this that these those please kindly just so then than about up out very really quite also which what who whom whose how when where why give me".split(" "));
function normExact(t) {
  return t.trim().toLowerCase().replace(/\s+/g, " ");
}
function words(t) {
  return normExact(t).replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter(Boolean);
}
function contentWords(ws) {
  return ws.filter((w) => !STOP.has(w));
}
function tf(ws) {
  const m = {};
  ws.forEach((w) => {
    m[w] = (m[w] || 0) + 1;
  });
  return m;
}
function cosine(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (const k in a) {
    na += a[k] * a[k];
    if (b[k]) dot += a[k] * b[k];
  }
  for (const k in b) nb += b[k] * b[k];
  if (!na || !nb) return 0;
  return dot / Math.sqrt(na * nb);
}
function hashInt(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h);
}
const est = (n) => Math.max(1, Math.round(n * 1.3));
const easeInOut = (t) => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
function formatUSD(x) {
  if (x >= 1) return "$" + x.toFixed(2);
  if (x >= 0.01) return "$" + x.toFixed(4);
  return "$" + x.toFixed(6);
}
function evaluate(text, history, model) {
  const ws = words(text);
  const contentTf = tf(contentWords(ws));
  const userTokens = est(ws.length);
  const norm = normExact(text);
  const exact = history.find((h) => h.norm === norm);
  let best = null, bestSim = 0;
  for (const h of history) {
    const sim = cosine(contentTf, h.contentTf);
    if (sim > bestSim) {
      bestSim = sim;
      best = h;
    }
  }
  const price = model;
  const outputTokens = 140 + hashInt(norm) % 360;
  const baselineCost = ((PREFIX_TOKENS + userTokens) * price.in + outputTokens * price.out) / 1e6;
  if (exact) return { outcome: "exact", text, norm, contentTf, similarity: 1, matched: exact, userTokens, outputTokens, tokensToModel: 0, fillerTokens: 0, prefixWarm: true, baselineCost, actualCost: 0, saved: baselineCost };
  if (best && bestSim >= SEMANTIC_THRESHOLD) return { outcome: "semantic", text, norm, contentTf, similarity: bestSim, matched: best, userTokens, outputTokens, tokensToModel: 0, fillerTokens: 0, prefixWarm: true, baselineCost, actualCost: 0, saved: best.baselineCost };
  const fillerTokens = Math.round((ws.length - contentWords(ws).length) * 1.3);
  const keptUser = Math.max(1, userTokens - fillerTokens);
  const prefixWarm = history.length > 0;
  const actualInput = PREFIX_TOKENS * (prefixWarm ? price.cached : price.in) + keptUser * price.in;
  const actualCost = (actualInput + outputTokens * price.out) / 1e6;
  return { outcome: "miss", text, norm, contentTf, similarity: bestSim, matched: best, userTokens, outputTokens, tokensToModel: PREFIX_TOKENS + keptUser, fillerTokens, prefixWarm, baselineCost, actualCost, saved: Math.max(0, baselineCost - actualCost) };
}
function useTween(value, ms = 900) {
  const [d, setD] = useState(value);
  const prev = useRef(value);
  useEffect(() => {
    const from = prev.current, to = value, t0 = performance.now();
    let raf;
    const tick = (now) => {
      const p = Math.min(1, (now - t0) / ms);
      setD(from + (to - from) * easeInOut(p));
      if (p < 1) raf = requestAnimationFrame(tick);
      else prev.current = to;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return d;
}
const NODES = [
  { k: "ingest", t: "Question arrives", tech: "request in", phase: 0 },
  { k: "exact", t: "Asked this before?", tech: "exact-match cache", phase: 0 },
  { k: "sem", t: "Asked anything like it?", tech: "similar-match cache", phase: 0 },
  { k: "comp", t: "Trim wasted words", tech: "compression", phase: 1 },
  { k: "prefix", t: "Reuse the setup", tech: "provider cache", phase: 1 },
  { k: "model", t: "Ask the AI", tech: "the pricey part", phase: 1, paid: true },
  { k: "store", t: "Remember it", tech: "save for next time", phase: 2 }
];
const PHASES = [
  { span: 3, label: "\u2460 Can we reuse an answer?" },
  { span: 3, label: "\u2461 If not, run it cheaply" },
  { span: 1, label: "\u2462 Remember" }
];
function nodeHint(i, rec) {
  if (!rec) return "";
  const hit = rec.outcome !== "miss";
  switch (i) {
    case 0:
      return "question in";
    case 1:
      return rec.outcome === "exact" ? "exact match \u2713" : "no exact match";
    case 2:
      return rec.matched ? rec.outcome === "semantic" ? Math.round(rec.similarity * 100) + "% alike \u2713" : "only " + Math.round(rec.similarity * 100) + "% alike" : "nothing yet";
    case 3:
      return hit ? "skipped" : "cut " + rec.fillerTokens + " words";
    case 4:
      return hit ? "skipped" : rec.prefixWarm ? "reused, near-free" : "first time";
    case 5:
      return hit ? "skipped \u2713" : "runs once";
    case 6:
      return hit ? "skipped" : "saved \u2713";
    default:
      return "";
  }
}
function narrate(rec, animating) {
  if (!rec) return { tone: "", text: /* @__PURE__ */ React.createElement("span", null, "Type a question below and press ", /* @__PURE__ */ React.createElement("strong", null, "Send"), ". Brevitas will check whether it can reuse a past answer before paying the AI to think again.") };
  if (rec.outcome === "exact") return { tone: "free", text: /* @__PURE__ */ React.createElement("span", null, "You already asked this ", /* @__PURE__ */ React.createElement("strong", null, "exact"), " question. Brevitas hands back the saved answer instantly \u2014 ", /* @__PURE__ */ React.createElement("span", { className: "free" }, "no AI call, ", formatUSD(rec.saved), " saved.")) };
  if (rec.outcome === "semantic") return { tone: "free", text: /* @__PURE__ */ React.createElement("span", null, "This is a ", /* @__PURE__ */ React.createElement("strong", null, "reworded version"), " of an earlier question \u2014 close enough to reuse that answer. ", /* @__PURE__ */ React.createElement("span", { className: "free" }, "No AI call, ", formatUSD(rec.saved), " saved.")) };
  if (animating) return { tone: "paid", text: /* @__PURE__ */ React.createElement("span", null, "A ", /* @__PURE__ */ React.createElement("strong", null, "brand-new"), " question \u2014 nothing to reuse. Brevitas trims the wasted words and reuses the repeated setup, then asks the AI at the lowest price.") };
  return { tone: "paid", text: /* @__PURE__ */ React.createElement("span", null, "New question, so it had to run. Even so, Brevitas paid ", /* @__PURE__ */ React.createElement("strong", null, formatUSD(rec.actualCost)), " instead of ", formatUSD(rec.baselineCost), " \u2014 and the answer is now saved for next time.") };
}
function Stage({ rec, frame }) {
  const hit = rec && rec.outcome !== "miss";
  const f = frame;
  return /* @__PURE__ */ React.createElement("div", { className: "stage" }, /* @__PURE__ */ React.createElement("div", { className: "stage-inner", id: "stageInner" }, /* @__PURE__ */ React.createElement("div", { className: "phase-row" }, PHASES.map((p, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "phase", style: { flex: p.span } }, p.label))), /* @__PURE__ */ React.createElement("div", { className: "node-row", id: "nodeRow" }, NODES.map((nd, i) => {
    const st = f && f.nodeState ? f.nodeState[i] || "" : "";
    const cls = ["node"];
    if (nd.paid) cls.push("paid");
    if (st === "active" && hit && i <= 2) cls.push("hit");
    else cls.push(st);
    if (hit && i >= 3 && f) cls.push("skipped");
    return /* @__PURE__ */ React.createElement("div", { key: nd.k, className: cls.join(" ") }, hit && i >= 3 && f && /* @__PURE__ */ React.createElement("span", { className: "pn-skiptag" }, "skipped"), /* @__PURE__ */ React.createElement("div", { className: "pn-k" }, nd.paid ? "$ costs money" : "step " + (i + 1)), /* @__PURE__ */ React.createElement("div", { className: "pn-t" }, nd.t), /* @__PURE__ */ React.createElement("div", { className: "pn-s" }, st === "active" || st === "done" ? nodeHint(i, rec) : nd.tech));
  })), /* @__PURE__ */ React.createElement("div", { className: "stage-overlay" }, f && /* @__PURE__ */ React.createElement(React.Fragment, null, f.nodes.map((nc, i) => {
    const on = f.nodeState[i] === "active" || f.nodeState[i] === "done";
    const sk = hit && i >= 3;
    return /* @__PURE__ */ React.createElement("div", { key: "s" + i, className: "stem" + (on ? hit && i <= 2 ? " hiton" : " on" : "") + (sk ? " skipped" : ""), style: { left: nc.x, top: nc.bottom, height: Math.max(0, f.railY - nc.bottom) } });
  }), /* @__PURE__ */ React.createElement("div", { className: "rail", style: { top: f.railY } }), /* @__PURE__ */ React.createElement("div", { className: "beam" + (hit ? " hit" : ""), style: { top: f.railY, width: Math.max(0, f.x) } }), f.nodes.map((nc, i) => {
    const on = f.nodeState[i] === "active" || f.nodeState[i] === "done";
    const cls = ["pin"];
    if (NODES[i].paid) cls.push("paid");
    if (on) cls.push(hit && i <= 2 ? "hiton" : "on");
    return /* @__PURE__ */ React.createElement("div", { key: "p" + i, className: cls.join(" "), style: { left: nc.x, top: f.railY } });
  }), f.trail.map((p, i) => /* @__PURE__ */ React.createElement("div", { key: "g" + i, className: "ghost" + (hit ? " hit" : ""), style: { transform: `translate(${p.x - 3.5}px, ${p.y - 3.5}px)`, opacity: (1 - i / f.trail.length) * 0.4, scale: String(1 - i * 0.08) } })), /* @__PURE__ */ React.createElement("div", { className: "pk" + (hit ? " hit" : ""), style: { transform: `translate(${f.x}px, ${f.y}px) translate(-50%, -50%)` } }, /* @__PURE__ */ React.createElement("span", { className: "pk-core" }), f.tok > 0 ? "your question" : "saved answer"), /* @__PURE__ */ React.createElement("div", { className: "replay-badge" + (f.replay ? " show" : ""), style: { left: f.endX, top: f.endY } }, "\u2713 ANSWER REUSED \xB7 FREE"), f.drop != null && /* @__PURE__ */ React.createElement("div", { className: "drop-tag", key: "drop", style: { left: f.dropX, top: f.dropTop } }, "trimming ", f.drop, " words")))));
}
const CHIPS = [
  "Explain how JWT authentication works.",
  "Give me a quick rundown of how JWT auth works, please.",
  "Write a haiku about the ocean.",
  "What is the capital of France?"
];
const SCENARIO = [
  "Summarize the Q3 revenue report and highlight the top three growth drivers.",
  "Summarize the Q3 revenue report and highlight the top three growth drivers.",
  "Give me a summary of the Q3 revenue report, focusing on its three biggest growth drivers.",
  "What were the main risks flagged in the Q3 report?",
  "List the key risks that were called out in the third-quarter report."
];
function DemoConsole() {
  const [modelIdx, setModelIdx] = useState(0);
  const [text, setText] = useState("");
  const [history, setHistory] = useState([]);
  const [current, setCurrent] = useState(null);
  const [frame, setFrame] = useState(null);
  const [busy, setBusy] = useState(false);
  const queue = useRef([]);
  const histRef = useRef([]);
  const rafRef = useRef(null);
  const reduced = useRef(false);
  useEffect(() => {
    reduced.current = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }, []);
  useEffect(() => () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
  }, []);
  const model = MODELS[modelIdx];
  const measure = () => {
    const inner = document.getElementById("stageInner");
    const row = document.getElementById("nodeRow");
    if (!inner || !row) return null;
    const base = inner.getBoundingClientRect();
    const nodes = Array.from(row.querySelectorAll(".node")).map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - base.left + r.width / 2, top: r.top - base.top, bottom: r.bottom - base.top };
    });
    if (nodes.length < 7) return null;
    const railY = Math.max(...nodes.map((n2) => n2.bottom)) + 48;
    return { nodes, railY, endX: nodes[6].x, endY: railY + 30 };
  };
  const commit = (rec) => {
    histRef.current = [rec, ...histRef.current];
    setHistory((h) => [rec, ...h]);
    setCurrent(rec);
    setFrame(null);
    const next = queue.current.shift();
    if (next != null) setTimeout(() => run(next), reduced.current ? 0 : 520);
    else setBusy(false);
  };
  const run = useCallback((prompt) => {
    const rec = evaluate(prompt, histRef.current, MODELS[modelIdx]);
    setCurrent(rec);
    setBusy(true);
    const geo = measure();
    if (!geo || reduced.current) {
      commit(rec);
      return;
    }
    const hit = rec.outcome !== "miss";
    const visit = hit ? [0, 1, 2] : [0, 1, 2, 3, 4, 5, 6];
    const segs = [];
    let t = 0;
    for (let k = 0; k < visit.length; k++) {
      if (k > 0) {
        segs.push({ type: "travel", a: visit[k - 1], b: visit[k], t0: t, t1: t + TRAVEL });
        t += TRAVEL;
      }
      segs.push({ type: "dwell", node: visit[k], t0: t, t1: t + DWELL });
      t += DWELL;
    }
    if (hit) {
      segs.push({ type: "express", t0: t, t1: t + EXPRESS });
      t += EXPRESS;
      segs.push({ type: "hold", t0: t, t1: t + DWELL });
      t += DWELL;
    }
    const total = t;
    const tokFull = hit ? rec.userTokens : PREFIX_TOKENS + rec.userTokens;
    const compressDwell = segs.find((s) => s.type === "dwell" && s.node === 3);
    const trail = [];
    const t0 = performance.now();
    const loop = (now) => {
      const e = now - t0;
      let seg = segs[segs.length - 1];
      for (const s of segs) {
        if (e >= s.t0 && e < s.t1) {
          seg = s;
          break;
        }
      }
      let x, y = geo.railY;
      if (seg.type === "travel") {
        const p = easeInOut((e - seg.t0) / TRAVEL);
        x = geo.nodes[seg.a].x + (geo.nodes[seg.b].x - geo.nodes[seg.a].x) * p;
      } else if (seg.type === "dwell") {
        x = geo.nodes[seg.node].x;
      } else if (seg.type === "express") {
        const p = easeInOut((e - seg.t0) / EXPRESS);
        x = geo.nodes[2].x + (geo.endX - geo.nodes[2].x) * p;
      } else {
        x = geo.endX;
      }
      let tok = tokFull;
      if (!hit && compressDwell) {
        if (e <= compressDwell.t0) tok = tokFull;
        else if (e >= compressDwell.t1) tok = rec.tokensToModel;
        else tok = Math.round(tokFull + (rec.tokensToModel - tokFull) * easeInOut((e - compressDwell.t0) / DWELL));
      } else if (hit && (seg.type === "express" || seg.type === "hold")) {
        tok = seg.type === "hold" ? 0 : Math.round(rec.userTokens * (1 - easeInOut((e - seg.t0) / EXPRESS)));
      }
      const nodeState = NODES.map((_, i) => {
        const d = segs.find((s) => s.type === "dwell" && s.node === i);
        if (!d) return "";
        if (e >= d.t1) return "done";
        if (e >= d.t0 - TRAVEL * 0.4) return "active";
        return "";
      });
      trail.unshift({ x, y });
      if (trail.length > 8) trail.pop();
      let drop = null, dropX = 0, dropTop = 0;
      if (!hit && compressDwell && rec.fillerTokens > 0 && e >= compressDwell.t0 && e < compressDwell.t0 + 1200) {
        drop = rec.fillerTokens;
        dropX = geo.nodes[3].x;
        dropTop = geo.nodes[3].top - 18;
      }
      setFrame({
        x,
        y,
        tok,
        railY: geo.railY,
        nodes: geo.nodes,
        nodeState,
        trail: trail.slice(1),
        endX: geo.endX,
        endY: geo.endY,
        replay: hit && (seg.type === "hold" || seg.type === "express" && (e - seg.t0) / EXPRESS > 0.82),
        drop,
        dropX,
        dropTop
      });
      if (e < total) rafRef.current = requestAnimationFrame(loop);
      else commit(rec);
    };
    rafRef.current = requestAnimationFrame(loop);
  }, [modelIdx]);
  const submit = (prompt) => {
    const p = (prompt != null ? prompt : text).trim();
    if (!p) return;
    setText("");
    if (busy) {
      queue.current.push(p);
      return;
    }
    run(p);
  };
  const loadScenario = () => {
    if (busy) return;
    const [first, ...rest] = SCENARIO;
    queue.current.push(...rest);
    run(first);
  };
  const reset = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    queue.current = [];
    histRef.current = [];
    setHistory([]);
    setCurrent(null);
    setFrame(null);
    setBusy(false);
    setText("");
  };
  const totals = history.reduce((a, r) => {
    a.saved += r.saved;
    a.spent += r.actualCost;
    a.baseline += r.baselineCost;
    if (r.outcome !== "miss") a.hits += 1;
    return a;
  }, { saved: 0, spent: 0, baseline: 0, hits: 0 });
  const n = history.length;
  const hitRate = n ? totals.hits / n * 100 : 0;
  const pctSaved = totals.baseline > 0 ? totals.saved / totals.baseline * 100 : 0;
  const tSaved = useTween(totals.saved), tRate = useTween(hitRate), tPct = useTween(pctSaved), tReq = useTween(n);
  const note = narrate(current, !!frame);
  return /* @__PURE__ */ React.createElement("div", { className: "demo-console" }, /* @__PURE__ */ React.createElement("div", { className: "demo-bar" }, /* @__PURE__ */ React.createElement("span", { className: "demo-dot" }), /* @__PURE__ */ React.createElement("span", { className: "demo-dot" }), /* @__PURE__ */ React.createElement("span", { className: "demo-dot" }), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", marginLeft: 8, fontSize: 12 } }, "brevitas \xB7 live"), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginRight: 4 } }, "your AI model:"), /* @__PURE__ */ React.createElement("div", { className: "seg", role: "group", "aria-label": "Model" }, MODELS.map((m, i) => /* @__PURE__ */ React.createElement("button", { key: m.key, className: i === modelIdx ? "on" : "", onClick: () => setModelIdx(i), type: "button" }, m.name)))), /* @__PURE__ */ React.createElement("div", { style: { padding: "clamp(18px, 3vw, 32px)", position: "relative", zIndex: 4 } }, /* @__PURE__ */ React.createElement("div", { className: "stat-grid", style: { marginBottom: 24 } }, /* @__PURE__ */ React.createElement("div", { className: "stat-tile" }, /* @__PURE__ */ React.createElement("div", { className: "v" }, Math.round(tReq)), /* @__PURE__ */ React.createElement("div", { className: "l" }, "QUESTIONS ASKED")), /* @__PURE__ */ React.createElement("div", { className: "stat-tile" }, /* @__PURE__ */ React.createElement("div", { className: "v", style: { color: hitRate > 0 ? "var(--signal)" : "var(--fg)" } }, tRate.toFixed(0), /* @__PURE__ */ React.createElement("span", { style: { fontSize: "0.5em", color: "var(--stone-2)" } }, "%")), /* @__PURE__ */ React.createElement("div", { className: "l" }, "ANSWERED FOR FREE")), /* @__PURE__ */ React.createElement("div", { className: "stat-tile" }, /* @__PURE__ */ React.createElement("div", { className: "v", style: { color: "var(--signal)" } }, formatUSD(tSaved)), /* @__PURE__ */ React.createElement("div", { className: "l" }, "MONEY SAVED")), /* @__PURE__ */ React.createElement("div", { className: "stat-tile" }, /* @__PURE__ */ React.createElement("div", { className: "v", style: { color: "var(--signal)" } }, tPct.toFixed(0), /* @__PURE__ */ React.createElement("span", { style: { fontSize: "0.5em", color: "var(--stone-2)" } }, "%")), /* @__PURE__ */ React.createElement("div", { className: "l" }, "OF THE BILL, GONE"))), /* @__PURE__ */ React.createElement("div", { className: "t-overline", style: { color: "var(--stone-2)", marginBottom: 8 } }, current ? current.outcome === "miss" ? "A NEW QUESTION \u2014 IT HAS TO RUN" : "SEEN IT BEFORE \u2014 REUSING THE ANSWER" : "THE PIPELINE \u2014 SEND A QUESTION TO WATCH IT WORK"), /* @__PURE__ */ React.createElement(Stage, { rec: current, frame }), /* @__PURE__ */ React.createElement("div", { className: "narrator " + note.tone, style: { marginTop: 8 } }, /* @__PURE__ */ React.createElement("span", { className: "n-ico" }, note.tone === "free" ? "\u2713" : note.tone === "paid" ? "\u2192" : "?"), /* @__PURE__ */ React.createElement("div", { className: "n-txt" }, note.text)), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 22 } }, /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "demo-input",
      placeholder: "Ask anything \u2014 e.g. 'Explain how JWT authentication works.' Then send it again, or reword it, and watch what happens.",
      value: text,
      onChange: (e) => setText(e.target.value),
      onKeyDown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          submit();
        }
      },
      rows: 2
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 12, marginTop: 14, flexWrap: "wrap", alignItems: "center" } }, /* @__PURE__ */ React.createElement(Button, { variant: "primary", onClick: () => submit(), arrow: true }, busy ? "Queue" : "Send"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "chip", onClick: loadScenario, disabled: busy, style: { opacity: busy ? 0.5 : 1 } }, "\u25B6 Play a 5-question example"), /* @__PURE__ */ React.createElement("button", { type: "button", className: "chip", onClick: reset }, "Reset"), /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, "prices shown are ", model.badge, "'s real published rates")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap", alignItems: "center" } }, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, "try:"), CHIPS.map((c, i) => /* @__PURE__ */ React.createElement("button", { key: i, type: "button", className: "chip", onClick: () => submit(c), disabled: busy, style: { opacity: busy ? 0.5 : 1, fontSize: 11 } }, c.length > 42 ? c.slice(0, 42) + "\u2026" : c)))), history.length > 0 && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 30 } }, /* @__PURE__ */ React.createElement("div", { className: "t-overline", style: { color: "var(--stone-2)", marginBottom: 6 } }, "WHAT HAPPENED, QUESTION BY QUESTION"), history.map((r, i) => /* @__PURE__ */ React.createElement("div", { key: n - i, className: "ledger-row" }, /* @__PURE__ */ React.createElement("div", { className: "lr-num t-mono", style: { color: "var(--stone)", fontSize: 12 } }, n - i), /* @__PURE__ */ React.createElement("div", { className: "badge " + r.outcome }, r.outcome === "exact" ? "REUSED \xB7 FREE" : r.outcome === "semantic" ? "REUSED \xB7 FREE" : "RAN IT"), /* @__PURE__ */ React.createElement("div", { style: { minWidth: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "t-body", style: { fontSize: 14, color: "var(--fg)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, r.text), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { fontSize: 11, color: "var(--stone)", marginTop: 3 } }, r.outcome === "miss" ? "asked the AI \xB7 paid " + formatUSD(r.actualCost) : r.outcome === "semantic" ? "reworded match (" + Math.round(r.similarity * 100) + "% alike) \xB7 no AI call" : "exact repeat \xB7 no AI call")), /* @__PURE__ */ React.createElement("div", { className: "lr-save", style: { textAlign: "right" } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono tabular", style: { color: r.saved > 0 ? "var(--signal)" : "var(--stone)", fontSize: 14 } }, r.saved > 0 ? "\u2212" + formatUSD(r.saved) : "$0"), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { fontSize: 10, color: "var(--stone)" } }, "saved"))))), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { fontSize: 11, color: "var(--stone)", marginTop: 24, lineHeight: 1.6 } }, "A simplified illustration. The real product matches reworded questions with a language model rather than word overlap, only reuses an answer when it's confident, keeps everything encrypted and private to you, and never mixes one AI model's answers with another's. Dollar figures use each provider's published prices.")));
}
function DemoPage() {
  useFadeUpReveal();
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, { current: "demo" }), /* @__PURE__ */ React.createElement(SectionShell, null, /* @__PURE__ */ React.createElement(Overline, { dot: true }, "INTERACTIVE DEMO"), /* @__PURE__ */ React.createElement("h1", { className: "t-display fade-up in", style: { margin: "24px 0 24px", maxWidth: 1e3 } }, "Every AI answer costs money. ", /* @__PURE__ */ React.createElement("em", { style: { fontStyle: "italic", color: "var(--bronze)" } }, "Most don't have to.")), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg fade-up delay-1 in", style: { maxWidth: 720, marginBottom: 0 } }, "When your app asks an AI the same thing twice \u2014 or something close to it \u2014 you normally pay full price every time. Brevitas catches the repeats and hands back the saved answer for free. Type a question below, then ask it again or in different words, and watch it happen.")), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 0, paddingBottom: "clamp(40px, 6vh, 72px)" } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("div", { className: "how-strip fade-up" }, /* @__PURE__ */ React.createElement("div", { className: "how-step" }, /* @__PURE__ */ React.createElement("div", { className: "how-num" }, "1"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Check if we've answered it"), /* @__PURE__ */ React.createElement("p", null, "Same question as before? Or a reworded version of one? If so, the saved answer comes straight back \u2014 instantly, for free."))), /* @__PURE__ */ React.createElement("div", { className: "how-step" }, /* @__PURE__ */ React.createElement("div", { className: "how-num" }, "2"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "If it's new, run it cheaply"), /* @__PURE__ */ React.createElement("p", null, "Trim the filler words, reuse the repeated setup the model already saw, and only then pay for the AI to think."))), /* @__PURE__ */ React.createElement("div", { className: "how-step" }, /* @__PURE__ */ React.createElement("div", { className: "how-num" }, "3"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Remember the answer"), /* @__PURE__ */ React.createElement("p", null, "Every fresh answer is saved, so the next time that question comes up, it's free too.")))))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("div", { className: "fade-up in" }, /* @__PURE__ */ React.createElement(DemoConsole, null)))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { background: "var(--ink-2)", borderTop: "1px solid var(--line)", borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 820 } }, /* @__PURE__ */ React.createElement("h2", { className: "t-h1", style: { marginTop: 0, marginBottom: 20 } }, "That's one question. Now picture your whole app."), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg", style: { marginBottom: 40, maxWidth: 680 } }, "Real apps ask the same things constantly \u2014 retries, loops, thousands of near-identical requests. Brevitas runs this on every one, typically cutting the bill 40\u201360%, with an honest receipt for every dollar saved."), /* @__PURE__ */ React.createElement(WaitlistInput, { variant: "inline", source: "demo-final" }))), /* @__PURE__ */ React.createElement(Footer, null));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(DemoPage, null));

})();
