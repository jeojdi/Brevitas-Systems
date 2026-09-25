const { useState, useEffect, useRef, useCallback } = React;
function initMatrixCanvas(canvasId, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return () => {
  };
  const ctx = canvas.getContext("2d");
  const cursorFollow = !!(opts && opts.cursor);
  const CELL = 12;
  const CHAR_POOL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$%&*<>{}[]|/\\^~\xB1\xA7\u0398\u039B\u039E\u03A3\u03A9\u03B1\u03B2\u03B3\u03B4";
  const randChar = () => CHAR_POOL[Math.floor(Math.random() * CHAR_POOL.length)];
  const BASE_OP = 0.06;
  const DIR_GLYPHS = [">", "\\", "v", "/", "<", "\\", "^", "/"];
  function dirGlyph(dx, dy) {
    let o = Math.round(Math.atan2(dy, dx) / (Math.PI / 4));
    if (o < 0) o += 8;
    return DIR_GLYPHS[o % 8];
  }
  const RING_GLYPHS = ["-", "\\", "|", "/", "-", "\\", "|", "/"];
  function ringGlyph(dx, dy) {
    let o = Math.round((Math.atan2(dy, dx) + Math.PI / 2) / (Math.PI / 4));
    if (o < 0) o += 8;
    return RING_GLYPHS[o % 8];
  }
  const FLOW_POOL = ".-_\xB7:~";
  const baseChar = () => cursorFollow ? FLOW_POOL[Math.floor(Math.random() * FLOW_POOL.length)] : randChar();
  const CURSOR_R = 74;
  const CURSOR_AMP = 1;
  const CURSOR_EASE = 0.3;
  const CURSOR_FOCUS = 1.2;
  const CURSOR_CELL_DECAY = 0.018;
  const CURSOR_GLOW_UP = 0.18;
  const CURSOR_GLOW_DOWN = 0.08;
  const CURSOR_IDLE_MS = 650;
  const CURSOR_DASH_AT = 0.45;
  const FLOW_FREQ = 0.1;
  const FLOW_SPEED = 9e-3;
  const FLOW_DEPTH = 0.38;
  const TRAIL_LEN = 34;
  const TRAIL_TAPER = 0.58;
  const TRAIL_WOBBLE = 8;
  const CLICK_MAXR = 228;
  const CLICK_LIFE_MS = 1300;
  const CLICK_AMP = 1;
  const CLICK_RING_COUNT = 3;
  const CLICK_RING_GAP = 28;
  const CLICK_RING_THICKNESS = 16;
  const CLICK_CENTER_LIFE_MS = 340;
  const CLICK_CENTER_MAXR = 58;
  let clickBursts = [];
  const AMBIENT = cursorFollow ? 0.07 : 1;
  const WAVE_THICKNESS = 48;
  const WAVE_AMP_MIN = 0.45;
  const WAVE_AMP_MAX = 0.68;
  const WAVE_SPEED_MIN = 1.2;
  const WAVE_SPEED_MAX = 2.8;
  const WAVE_R_MIN = 120;
  const WAVE_R_MAX = 280;
  const WAVE_DECAY = 0.028;
  const WAVE_MUTATE = 0.4;
  const WAVE_SPAWN = 0.1;
  const POINT_THICKNESS = 32;
  const POINT_AMP = 0.82;
  const POINT_SPEED_MIN = 0.25;
  const POINT_SPEED_MAX = 0.5;
  const POINT_R_MIN = 200;
  const POINT_R_MAX = 380;
  const POINT_DECAY = 0.05;
  const POINT_SPAWN_MS_MIN = 1200;
  const POINT_SPAWN_MS_MAX = 2800;
  function WaveRipple() {
    this.x = Math.random() * canvas.width;
    this.y = Math.random() * canvas.height;
    this.radius = 0;
    this.maxRadius = WAVE_R_MIN + Math.random() * (WAVE_R_MAX - WAVE_R_MIN);
    this.speed = WAVE_SPEED_MIN + Math.random() * (WAVE_SPEED_MAX - WAVE_SPEED_MIN);
    this.amplitude = WAVE_AMP_MIN + Math.random() * (WAVE_AMP_MAX - WAVE_AMP_MIN);
  }
  WaveRipple.prototype.intensityAt = function(px, py) {
    const edge = Math.abs(Math.sqrt((px - this.x) ** 2 + (py - this.y) ** 2) - this.radius);
    if (edge >= WAVE_THICKNESS) return 0;
    return 0.5 * (1 + Math.cos(Math.PI * edge / WAVE_THICKNESS)) * (1 - this.radius / this.maxRadius) * this.amplitude;
  };
  Object.defineProperty(WaveRipple.prototype, "dead", { get() {
    return this.radius >= this.maxRadius;
  } });
  function PointRipple() {
    this.x = Math.random() * canvas.width;
    this.y = Math.random() * canvas.height;
    this.radius = 0;
    this.maxRadius = POINT_R_MIN + Math.random() * (POINT_R_MAX - POINT_R_MIN);
    this.speed = POINT_SPEED_MIN + Math.random() * (POINT_SPEED_MAX - POINT_SPEED_MIN);
  }
  PointRipple.prototype.intensityAt = function(px, py) {
    const edge = Math.abs(Math.sqrt((px - this.x) ** 2 + (py - this.y) ** 2) - this.radius);
    if (edge >= POINT_THICKNESS) return 0;
    return 0.5 * (1 + Math.cos(Math.PI * edge / POINT_THICKNESS)) * POINT_AMP;
  };
  Object.defineProperty(PointRipple.prototype, "dead", { get() {
    return this.radius >= this.maxRadius;
  } });
  let cols, rows, cells, waveRipples, pointRipples, raf, resizeTimer, spawnTimer;
  let curTX = null, curTY = null, curX = null, curY = null, curActive = false, curGlow = 0, lastMove = 0;
  let trail = [], frameN = 0;
  function init() {
    canvas.width = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
    cols = Math.ceil(canvas.width / CELL);
    rows = Math.ceil(canvas.height / CELL);
    waveRipples = [];
    pointRipples = [];
    cells = Array.from({ length: cols * rows }, () => ({ char: baseChar(), waveOp: 0, pointOp: 0, curOp: 0 }));
    const nInit = Math.round(14 * AMBIENT);
    for (let i = 0; i < nInit; i++) {
      const r = new WaveRipple();
      r.radius = Math.random() * r.maxRadius * 0.7;
      waveRipples.push(r);
    }
  }
  function schedulePointRipple() {
    const delay = (POINT_SPAWN_MS_MIN + Math.random() * (POINT_SPAWN_MS_MAX - POINT_SPAWN_MS_MIN)) / AMBIENT;
    spawnTimer = setTimeout(() => {
      pointRipples.push(new PointRipple());
      schedulePointRipple();
    }, delay);
  }
  function draw() {
    frameN++;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (Math.random() < WAVE_SPAWN * AMBIENT) waveRipples.push(new WaveRipple());
    for (let i = waveRipples.length - 1; i >= 0; i--) {
      waveRipples[i].radius += waveRipples[i].speed;
      if (waveRipples[i].dead) waveRipples.splice(i, 1);
    }
    for (let i = pointRipples.length - 1; i >= 0; i--) {
      pointRipples[i].radius += pointRipples[i].speed;
      if (pointRipples[i].dead) pointRipples.splice(i, 1);
    }
    const now = performance.now();
    if (cursorFollow) {
      const moving = curActive && curTX != null && now - lastMove < CURSOR_IDLE_MS;
      if (moving) {
        if (curX == null) {
          curX = curTX;
          curY = curTY;
        }
        curX += (curTX - curX) * CURSOR_EASE;
        curY += (curTY - curY) * CURSOR_EASE;
        curGlow = Math.min(1, curGlow + CURSOR_GLOW_UP);
        trail.unshift({ x: curX, y: curY });
        if (trail.length > TRAIL_LEN) trail.pop();
      } else {
        curGlow = Math.max(0, curGlow - CURSOR_GLOW_DOWN);
        if (trail.length) {
          trail.pop();
          trail.pop();
        }
        if (curGlow <= 0.01) {
          trail.length = 0;
          curX = null;
        }
      }
      for (let i = clickBursts.length - 1; i >= 0; i--) {
        const b = clickBursts[i], age = now - b.t0;
        if (age >= CLICK_LIFE_MS) {
          clickBursts.splice(i, 1);
          continue;
        }
        const progress = age / CLICK_LIFE_MS;
        b.radius = CLICK_MAXR * (1 - Math.pow(1 - progress, 2.6));
        b.alpha = Math.pow(1 - progress, 1.25) * CLICK_AMP;
      }
    }
    ctx.font = `${CELL - 2}px "JetBrains Mono","Courier New",monospace`;
    for (let row = 0; row < rows; row++) {
      const py = row * CELL + CELL;
      for (let col = 0; col < cols; col++) {
        const px = col * CELL + CELL * 0.5;
        const cell = cells[row * cols + col];
        let wTotal = 0, wPeak = 0;
        for (const r of waveRipples) {
          const i = r.intensityAt(px, py);
          if (i === 0) continue;
          wTotal = Math.min(1, wTotal + i);
          if (i > wPeak) wPeak = i;
        }
        if (wTotal > 0.03) {
          cell.waveOp = Math.max(cell.waveOp, wTotal);
          if (wPeak > WAVE_MUTATE && Math.random() < 0.35) cell.char = randChar();
        } else {
          cell.waveOp = Math.max(0, cell.waveOp - WAVE_DECAY);
        }
        let pTotal = 0;
        for (const r of pointRipples) {
          const i = r.intensityAt(px, py);
          if (i > 0) pTotal = Math.min(1, pTotal + i);
        }
        if (pTotal > 0.02) {
          cell.pointOp = Math.max(cell.pointOp, pTotal);
          if (Math.random() < 0.55) cell.char = randChar();
        } else {
          cell.pointOp = Math.max(0, cell.pointOp - POINT_DECAY);
        }
        if (cursorFollow) {
          if (cell.curOp > 0) cell.curOp = Math.max(0, cell.curOp - CURSOR_CELL_DECAY);
          if (curGlow > 0.01 && trail.length) {
            let best = 0, bdx = 0, bdy = 0, bcd = 0;
            for (let k = 0; k < trail.length; k++) {
              const tp = trail[k], sway = k / TRAIL_LEN;
              const wob = Math.sin(now * 4e-3 + k * 0.55) * TRAIL_WOBBLE * sway;
              const dx = px - (tp.x + wob), dy = py - (tp.y - wob * 0.6);
              const rk = CURSOR_R * (1 - TRAIL_TAPER * sway);
              if (dx < -rk || dx > rk || dy < -rk || dy > rk) continue;
              const cd = Math.sqrt(dx * dx + dy * dy);
              if (cd >= rk) continue;
              const falloff = 0.5 * (1 + Math.cos(Math.PI * cd / rk));
              const g = Math.pow(falloff, CURSOR_FOCUS) * (1 - sway * 0.5);
              if (g > best) {
                best = g;
                bdx = dx;
                bdy = dy;
                bcd = cd;
              }
            }
            if (best > 0) {
              const flow = 1 - FLOW_DEPTH * 0.5 * (1 - Math.cos(bcd * FLOW_FREQ - now * FLOW_SPEED));
              const lit = best * CURSOR_AMP * curGlow * flow;
              if (lit > cell.curOp) cell.curOp = lit;
              cell.char = best > 0.06 && curGlow > CURSOR_DASH_AT ? dirGlyph(bdx, bdy) : "-";
            }
          }
          if (cell.curOp > 0 && curGlow <= CURSOR_DASH_AT) cell.char = "-";
        }
        let clkOp = 0;
        for (const b of clickBursts) {
          const bdx = px - b.x, bdy = py - b.y, bd = Math.sqrt(bdx * bdx + bdy * bdy);
          const age = now - b.t0;
          let burstOp = 0;
          for (let ring = 0; ring < CLICK_RING_COUNT; ring++) {
            const ringRadius = b.radius - ring * CLICK_RING_GAP;
            if (ringRadius <= 0) continue;
            const edge = Math.abs(bd - ringRadius);
            if (edge >= CLICK_RING_THICKNESS) continue;
            const ringFade = 1 - ring / (CLICK_RING_COUNT + 1);
            const ringOp = 0.5 * (1 + Math.cos(Math.PI * edge / CLICK_RING_THICKNESS)) * b.alpha * ringFade;
            if (ringOp > burstOp) burstOp = ringOp;
          }
          if (age < CLICK_CENTER_LIFE_MS) {
            const centerProgress = age / CLICK_CENTER_LIFE_MS;
            const centerRadius = CLICK_CENTER_MAXR * (0.35 + centerProgress * 0.65);
            if (bd < centerRadius) {
              const centerOp = 0.5 * (1 + Math.cos(Math.PI * bd / centerRadius)) * (1 - centerProgress) * 0.72;
              if (centerOp > burstOp) burstOp = centerOp;
            }
          }
          if (burstOp > clkOp) {
            clkOp = burstOp;
            if (burstOp > 0.12) cell.char = ringGlyph(bdx, bdy);
          }
        }
        const glowOp = cell.pointOp + cell.curOp + clkOp;
        const finalOp = Math.min(1, BASE_OP + cell.waveOp + glowOp);
        if (finalOp < 0.02) continue;
        ctx.fillStyle = `rgba(255,255,255,${finalOp.toFixed(3)})`;
        if (glowOp > 0.1) {
          ctx.shadowColor = "rgba(255,255,255,0.95)";
          ctx.shadowBlur = 13;
        }
        ctx.fillText(cell.char, col * CELL, py);
        if (glowOp > 0.1) ctx.shadowBlur = 0;
      }
    }
  }
  const FRAME_MS = 1e3 / (cursorFollow ? 60 : 30);
  const reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  let lastFrame = 0, inView = true, pageVisible = !document.hidden;
  function loop(ts) {
    raf = requestAnimationFrame(loop);
    if (ts - lastFrame < FRAME_MS) return;
    lastFrame = ts;
    draw();
  }
  function start() {
    if (raf == null && !reduceMotion) {
      lastFrame = 0;
      raf = requestAnimationFrame(loop);
    }
  }
  function stop() {
    if (raf != null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
  }
  function sync() {
    inView && pageVisible ? start() : stop();
  }
  const io = new IntersectionObserver(([e]) => {
    inView = e.isIntersecting;
    sync();
  }, { threshold: 0 });
  const onVisibility = () => {
    pageVisible = !document.hidden;
    sync();
  };
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      stop();
      init();
      reduceMotion ? draw() : start();
    }, 150);
  };
  const onMouseMove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) {
      curActive = false;
      return;
    }
    curTX = x * (canvas.width / rect.width);
    curTY = y * (canvas.height / rect.height);
    curActive = true;
    lastMove = performance.now();
  };
  const onMouseOut = (e) => {
    if (!e.relatedTarget && !e.toElement) {
      curActive = false;
      curX = null;
    }
  };
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
    clickBursts.push({ x: x * (canvas.width / rect.width), y: y * (canvas.height / rect.height), t0: performance.now(), radius: 0, alpha: CLICK_AMP });
    if (clickBursts.length > 6) clickBursts.shift();
  };
  window.addEventListener("resize", onResize);
  document.addEventListener("visibilitychange", onVisibility);
  if (cursorFollow) {
    window.addEventListener("mousemove", onMouseMove, { passive: true });
    document.addEventListener("mouseout", onMouseOut);
    window.addEventListener("pointerdown", onPointerDown, { passive: true });
  }
  io.observe(canvas);
  init();
  if (!cursorFollow) pointRipples.push(new PointRipple());
  if (!reduceMotion) {
    schedulePointRipple();
    start();
  } else {
    draw();
  }
  return () => {
    stop();
    io.disconnect();
    clearTimeout(spawnTimer);
    clearTimeout(resizeTimer);
    window.removeEventListener("resize", onResize);
    document.removeEventListener("visibilitychange", onVisibility);
    if (cursorFollow) {
      window.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseout", onMouseOut);
      window.removeEventListener("pointerdown", onPointerDown);
    }
  };
}
function useInView(opts = { threshold: 0.2 }) {
  const ref = useRef(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const io = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) {
        setInView(true);
        io.disconnect();
      }
    }, opts);
    io.observe(ref.current);
    return () => io.disconnect();
  }, []);
  return [ref, inView];
}
function useReducedMotion() {
  const [r, setR] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setR(mq.matches);
    const h = (e) => setR(e.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, []);
  return r;
}
function useCountUp(target, { duration = 800, start = 0, trigger = true } = {}) {
  const [val, setVal] = useState(start);
  useEffect(() => {
    if (!trigger) return;
    let raf;
    const t0 = performance.now();
    const ease = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    const tick = (now) => {
      const p = Math.min(1, (now - t0) / duration);
      setVal(start + (target - start) * ease(p));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, trigger]);
  return val;
}
function LogoMark({ size = 28, color }) {
  const s = size;
  const style = { color: color || "currentColor" };
  return /* @__PURE__ */ React.createElement("svg", { width: s, height: s, viewBox: "0 0 28 28", style, "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("rect", { x: "1", y: "1", width: "26", height: "26", fill: "none", stroke: "currentColor", strokeWidth: "1.5" }), /* @__PURE__ */ React.createElement("line", { x1: "14", y1: "4", x2: "14", y2: "24", stroke: "currentColor", strokeWidth: "1.5" }), /* @__PURE__ */ React.createElement("line", { x1: "4", y1: "14", x2: "24", y2: "14", stroke: "currentColor", strokeWidth: "1.5" }));
}
function ArrowRight({ size = 14 }) {
  return /* @__PURE__ */ React.createElement("span", { className: "arrow", "aria-hidden": "true" }, "\u2192");
}
function Overline({ children, dot = false, className = "" }) {
  return /* @__PURE__ */ React.createElement("div", { className: `overline t-overline ${className}` }, dot && /* @__PURE__ */ React.createElement("span", { className: "overline-dot" }), /* @__PURE__ */ React.createElement("span", null, children));
}
function Button({ variant = "primary", href, onClick, children, arrow = true, className = "", target, rel }) {
  const cls = `btn btn-${variant} ${className}`;
  const content = /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", null, children), arrow && /* @__PURE__ */ React.createElement("span", { className: "arrow", "aria-hidden": "true" }, "\u2192"));
  const relValue = rel != null ? rel : target === "_blank" ? "noopener noreferrer" : void 0;
  if (href) return /* @__PURE__ */ React.createElement("a", { href, className: cls, onClick, target, rel: relValue }, content);
  return /* @__PURE__ */ React.createElement("button", { className: cls, onClick, type: "button" }, content);
}
function SectionShell({ overline, overlineDot, rule = false, children, className = "", id, tight = false }) {
  return /* @__PURE__ */ React.createElement("section", { id, className: `section ${tight ? "section--tight" : ""} ${className}` }, /* @__PURE__ */ React.createElement("div", { className: "container" }, rule && /* @__PURE__ */ React.createElement("hr", { className: "rule", style: { marginBottom: 48 } }), overline && /* @__PURE__ */ React.createElement(Overline, { dot: overlineDot }, overline), /* @__PURE__ */ React.createElement("div", { style: { marginTop: overline ? 32 : 0 } }, children)));
}
function StatCard({ value, label, sub, variant = "default", delta, onInView = true }) {
  const [ref, inView] = useInView();
  const numericTarget = typeof value === "number" ? value : parseFloat(String(value).replace(/[^-0-9.]/g, ""));
  const suffix = typeof value === "string" ? value.match(/%$/) ? "%" : "" : "%";
  const prefix = typeof value === "string" && value.startsWith("\u2013") ? "\u2013" : "";
  const count = useCountUp(numericTarget, { duration: 900, trigger: inView });
  const display = isNaN(numericTarget) ? value : `${prefix}${count.toFixed(1).replace(/\.0$/, "")}${suffix}`;
  if (variant === "inline") {
    return /* @__PURE__ */ React.createElement("div", { ref, className: "stat-inline", style: { display: "flex", flexDirection: "column", gap: 4, minWidth: 140 } }, /* @__PURE__ */ React.createElement("div", { className: "serif tabular", style: { fontSize: "clamp(32px, 3.6vw, 44px)", fontWeight: 300, lineHeight: 1, color: "var(--fg)", letterSpacing: "-0.01em" } }, display), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone-2)", marginTop: 6 } }, label));
  }
  const isEmph = variant === "emphasis";
  return /* @__PURE__ */ React.createElement("div", { ref, className: "card", style: { padding: isEmph ? "40px 32px" : 28 } }, /* @__PURE__ */ React.createElement("div", { className: "serif tabular", style: {
    fontSize: isEmph ? "clamp(64px, 7vw, 96px)" : "clamp(40px, 4.5vw, 56px)",
    fontWeight: 300,
    lineHeight: 1,
    letterSpacing: "-0.02em",
    color: "var(--fg)"
  } }, display), /* @__PURE__ */ React.createElement("hr", { className: "rule", style: { margin: "20px 0 16px", width: 40, borderColor: "var(--line)" } }), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--fg-dim)" } }, label), sub && /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", marginTop: 4 } }, sub));
}
function TechniqueCard({ index, title, body, demoLink }) {
  return /* @__PURE__ */ React.createElement("div", { className: "card", style: { display: "flex", flexDirection: "column", gap: 16, minHeight: 260 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)" } }, String(index).padStart(2, "0")), /* @__PURE__ */ React.createElement("div", { className: "serif", style: { fontSize: 24, fontWeight: 400, lineHeight: 1.22, letterSpacing: "-0.01em" } }, title), /* @__PURE__ */ React.createElement("div", { className: "t-body", style: { color: "var(--bone-dim)", flex: 1 } }, body), demoLink && /* @__PURE__ */ React.createElement("a", { href: demoLink, className: "t-mono link", style: { color: "var(--bronze)", marginTop: 8 } }, "see it \u2192"));
}
function BenchmarkBadge({ letter, name, venue }) {
  return /* @__PURE__ */ React.createElement("div", { className: "card", style: { display: "flex", alignItems: "center", gap: 16, padding: "20px 24px", minWidth: 220 } }, /* @__PURE__ */ React.createElement("div", { style: {
    width: 44,
    height: 44,
    border: "1.5px solid var(--stone-2)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontFamily: "Newsreader, serif",
    fontWeight: 400,
    fontSize: 22,
    color: "var(--fg)"
  } }, letter), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: { fontWeight: 500, fontSize: 15, color: "var(--fg)" } }, name), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone-2)", fontSize: 12 } }, venue)));
}
function CodeBlock({ children, copyable = false, language = "python", filename }) {
  const [copied, setCopied] = useState(false);
  const doCopy = () => {
    var _a, _b;
    try {
      const text = typeof children === "string" ? children : (_b = (_a = children == null ? void 0 : children.props) == null ? void 0 : _a.children) != null ? _b : "";
      navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
    }
  };
  return /* @__PURE__ */ React.createElement("div", { style: {
    background: "var(--ink-2)",
    border: "1px solid var(--line)",
    borderRadius: 4,
    position: "relative",
    overflow: "hidden"
  } }, filename && /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: {
    padding: "10px 20px",
    borderBottom: "1px solid var(--line)",
    color: "var(--stone-2)",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center"
  } }, /* @__PURE__ */ React.createElement("span", null, filename), copyable && /* @__PURE__ */ React.createElement("button", { onClick: doCopy, className: "t-mono", style: { background: "transparent", border: 0, color: copied ? "var(--signal)" : "var(--stone-2)", cursor: "pointer", padding: 0 } }, copied ? "\u2713 copied" : "\u29C9 copy")), /* @__PURE__ */ React.createElement("pre", { style: {
    margin: 0,
    padding: 20,
    fontFamily: "JetBrains Mono, ui-monospace, monospace",
    fontSize: 14,
    lineHeight: 1.6,
    color: "var(--bone-dim)",
    overflowX: "auto"
  } }, /* @__PURE__ */ React.createElement("code", null, children)));
}
function syntaxPython(src) {
  const lines = src.split("\n");
  return lines.map((line, i) => {
    let codePart = line, commentPart = "";
    const hashIdx = (() => {
      let inStr = null;
      for (let j = 0; j < line.length; j++) {
        const c = line[j];
        if (inStr) {
          if (c === inStr && line[j - 1] !== "\\") inStr = null;
        } else if (c === '"' || c === "'") inStr = c;
        else if (c === "#") return j;
      }
      return -1;
    })();
    if (hashIdx >= 0) {
      codePart = line.slice(0, hashIdx);
      commentPart = line.slice(hashIdx);
    }
    const parts = [];
    const re = /(\s+)|("[^"]*"|'[^']*')|\b(import|from|def|return|class|if|else|for|in|as|with|pass|None|True|False|await|async)\b|(\b\d[\d_.]*\b)|([A-Za-z_][A-Za-z0-9_]*)|(.)/g;
    let m, idx = 0;
    while ((m = re.exec(codePart)) !== null) {
      if (m[1]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++ }, m[1]));
      else if (m[2]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++, style: { color: "var(--signal)" } }, m[2]));
      else if (m[3]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++, style: { color: "var(--bone)", fontWeight: 500 } }, m[3]));
      else if (m[4]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++, style: { color: "var(--stone-2)" } }, m[4]));
      else if (m[5]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++, style: { color: "var(--bone-dim)" } }, m[5]));
      else if (m[6]) parts.push(/* @__PURE__ */ React.createElement("span", { key: idx++ }, m[6]));
    }
    if (commentPart) parts.push(/* @__PURE__ */ React.createElement("span", { key: "c" + i, style: { color: "var(--stone)" } }, commentPart));
    return /* @__PURE__ */ React.createElement("div", { key: i }, parts.length ? parts : "\xA0");
  });
}
function CodeBlockPy({ source, filename, copyable = true }) {
  return /* @__PURE__ */ React.createElement(CodeBlock, { filename, copyable }, /* @__PURE__ */ React.createElement(React.Fragment, null, syntaxPython(source)));
}
const DEFAULT_INSTALL_COMMANDS = [
  { label: "macOS", prompt: "$", command: "brew install Brevitas-ai/brevitas/bvx && bvx login && bvx install" },
  { label: "Linux", prompt: "$", command: "brew install Brevitas-ai/brevitas/bvx && bvx login && bvx install" },
  { label: "Windows", prompt: ">", command: "irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex; if ($?) { bvx login; if ($?) { bvx install } }" }
];
function InstallCommand({ commands, command }) {
  const list = commands || (command ? [{ label: "", prompt: "$", command }] : DEFAULT_INSTALL_COMMANDS);
  const [active, setActive] = useState(0);
  const [copied, setCopied] = useState(false);
  const [copyAnimation, setCopyAnimation] = useState(0);
  const copyResetTimer = useRef(null);
  const current = list[active] || list[0];
  useEffect(() => () => clearTimeout(copyResetTimer.current), []);
  const doCopy = async () => {
    try {
      await navigator.clipboard.writeText(current.command);
      setCopied(true);
      setCopyAnimation((animation) => animation + 1);
      clearTimeout(copyResetTimer.current);
      copyResetTimer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
    }
  };
  return /* @__PURE__ */ React.createElement("div", { className: "install-command", style: { display: "inline-flex", flexDirection: "column", gap: 10, maxWidth: "100%" } }, list.length > 1 && /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { display: "flex", gap: 10, fontSize: 16 } }, list.map((c, i) => /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      key: c.label,
      onClick: () => {
        clearTimeout(copyResetTimer.current);
        setActive(i);
        setCopied(false);
      },
      style: {
        background: i === active ? "var(--bronze)" : "var(--ink-2)",
        border: i === active ? "1px solid var(--bronze)" : "1px solid var(--line)",
        borderRadius: 10,
        padding: "10px 22px",
        color: i === active ? "#ffffff" : "var(--stone-2)",
        fontWeight: 600,
        letterSpacing: "0.01em",
        cursor: "pointer",
        transition: "all 0.15s ease"
      }
    },
    c.label
  ))), /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      onClick: doCopy,
      className: "t-mono install-command-button",
      title: copied ? "Install command copied" : "Copy install command",
      style: {
        display: "inline-flex",
        alignItems: "center",
        gap: 18,
        maxWidth: "100%",
        background: "var(--ink-2)",
        border: "1px solid var(--line)",
        borderRadius: 10,
        padding: "16px 22px",
        color: "var(--stone)",
        fontSize: 17,
        cursor: "pointer",
        textAlign: "left"
      }
    },
    /* @__PURE__ */ React.createElement("span", { "aria-hidden": "true", style: { color: "var(--bronze)", userSelect: "none" } }, current.prompt || "$"),
    /* @__PURE__ */ React.createElement("span", { style: { overflowX: "auto", whiteSpace: "nowrap", color: "var(--fg)" } }, current.command),
    /* @__PURE__ */ React.createElement(
      "span",
      {
        key: copied ? `copied-${copyAnimation}` : "copy",
        className: `install-command-copy-status${copied ? " is-copied" : ""}`,
        "aria-live": "polite",
        "aria-atomic": "true"
      },
      copied ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "install-command-copy-check", "aria-hidden": "true" }, "\u2713"), " Copied!") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { "aria-hidden": "true" }, "\u29C9"), " Copy")
    )
  ));
}
function WaitlistInput({ variant = "inline", source = "unknown" }) {
  const [email, setEmail] = useState("");
  const [expanded, setExpanded] = useState(variant === "full");
  const [state, setState] = useState("idle");
  const [err, setErr] = useState("");
  const [form, setForm] = useState({ name: "", company: "", role: "", building: "" });
  const emailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const submitEmail = (e) => {
    var _a;
    (_a = e == null ? void 0 : e.preventDefault) == null ? void 0 : _a.call(e);
    if (!emailValid) {
      setErr("Hmm, that email doesn't look right.");
      return;
    }
    setErr("");
    setExpanded(true);
  };
  const submitAll = async (e) => {
    var _a;
    (_a = e == null ? void 0 : e.preventDefault) == null ? void 0 : _a.call(e);
    if (!emailValid) {
      setErr("Hmm, that email doesn't look right.");
      return;
    }
    if (form.name.trim().length < 2) {
      setErr("Need your name to say hello.");
      return;
    }
    if (form.company.trim().length < 2) {
      setErr("What company is this for?");
      return;
    }
    if (!form.role) {
      setErr("Pick a role so we can triage.");
      return;
    }
    if (form.building.trim().length < 20) {
      setErr("A sentence or two helps us triage \u2014 what's the multi-agent use case?");
      return;
    }
    setErr("");
    setState("submitting");
    try {
      const response = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          name: form.name.trim(),
          company: form.company.trim(),
          role: form.role,
          notes: form.building.trim()
        })
      });
      const data = await response.json();
      if (response.ok && data.success) {
        setState("success");
      } else {
        setErr(data.error || "Failed to join waitlist. Please try again.");
        setState("idle");
      }
    } catch (error) {
      console.error("Waitlist submission error:", error);
      setErr("Connection error. Please try again.");
      setState("idle");
    }
  };
  if (state === "success") {
    return /* @__PURE__ */ React.createElement("div", { style: {
      border: "1px solid var(--line)",
      padding: variant === "full" ? "40px 32px" : "28px 24px",
      borderRadius: 4,
      background: "var(--ink-2)",
      maxWidth: variant === "full" ? 720 : 560
    } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, marginBottom: 12 } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--signal)", fontSize: 24 } }, "\u2713"), /* @__PURE__ */ React.createElement("div", { className: "serif", style: { fontSize: 28, fontWeight: 300, letterSpacing: "-0.01em" } }, "You're on the list.")), /* @__PURE__ */ React.createElement("div", { className: "t-body" }, "We'll email at ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, email.split("@")[1]), " when we have room to talk. In the meantime, watch for a monthly note \u2014 no more, usually less."));
  }
  if (variant === "footer-small") {
    return /* @__PURE__ */ React.createElement("form", { onSubmit: submitEmail, style: { display: "flex", gap: 6, alignItems: "stretch" } }, /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "email",
        className: "input",
        placeholder: "name@company.com",
        value: email,
        onChange: (e) => setEmail(e.target.value),
        style: { padding: "10px 14px", fontSize: 14 }
      }
    ), /* @__PURE__ */ React.createElement("button", { type: "submit", className: "btn btn-primary", style: { padding: "10px 16px", fontSize: 14 } }, "Join ", /* @__PURE__ */ React.createElement("span", { className: "arrow" }, "\u2192")));
  }
  return /* @__PURE__ */ React.createElement("form", { onSubmit: expanded ? submitAll : submitEmail, style: { maxWidth: variant === "full" ? 720 : 560 } }, !expanded && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "waitlist-row" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "email",
      className: "input",
      placeholder: "name@company.com",
      value: email,
      onChange: (e) => setEmail(e.target.value),
      "aria-label": "Work email"
    }
  ), /* @__PURE__ */ React.createElement("button", { type: "submit", className: "btn btn-primary" }, "Join now ", /* @__PURE__ */ React.createElement("span", { className: "arrow" }, "\u2192"))), err && /* @__PURE__ */ React.createElement("div", { className: "t-small", style: { marginTop: 10, color: "var(--bone-dim)" } }, "! ", err)), expanded && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 12 } }, /* @__PURE__ */ React.createElement("div", { className: "waitlist-row" }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "email",
      className: "input",
      placeholder: "name@company.com",
      value: email,
      onChange: (e) => setEmail(e.target.value),
      "aria-label": "Work email"
    }
  ), /* @__PURE__ */ React.createElement("span", { style: {
    display: "inline-flex",
    alignItems: "center",
    padding: "0 14px",
    border: "1px solid var(--line)",
    borderRadius: 2,
    color: emailValid ? "var(--signal)" : "var(--stone)"
  } }, emailValid ? "\u2713" : "")), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "input",
      placeholder: "Your name",
      value: form.name,
      onChange: (e) => setForm({ ...form, name: e.target.value })
    }
  ), /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "input",
      placeholder: "Company",
      value: form.company,
      onChange: (e) => setForm({ ...form, company: e.target.value })
    }
  ), /* @__PURE__ */ React.createElement(
    "select",
    {
      className: "input",
      value: form.role,
      onChange: (e) => setForm({ ...form, role: e.target.value }),
      style: { color: form.role ? "var(--fg)" : "var(--stone)" }
    },
    /* @__PURE__ */ React.createElement("option", { value: "" }, "Role"),
    /* @__PURE__ */ React.createElement("option", { value: "Founder / CEO" }, "Founder / CEO"),
    /* @__PURE__ */ React.createElement("option", { value: "CTO / Head of Engineering" }, "CTO / Head of Engineering"),
    /* @__PURE__ */ React.createElement("option", { value: "Engineering IC" }, "Engineering IC"),
    /* @__PURE__ */ React.createElement("option", { value: "Product / Platform Lead" }, "Product / Platform Lead"),
    /* @__PURE__ */ React.createElement("option", { value: "Investor" }, "Investor"),
    /* @__PURE__ */ React.createElement("option", { value: "Researcher" }, "Researcher"),
    /* @__PURE__ */ React.createElement("option", { value: "Other" }, "Other")
  ), /* @__PURE__ */ React.createElement(
    "textarea",
    {
      className: "input",
      placeholder: 'e.g., "Three-agent research pipeline on GPT-4o and Claude 4.6 \u2014 ~10M tokens/month, cost the main constraint."',
      value: form.building,
      onChange: (e) => setForm({ ...form, building: e.target.value }),
      rows: 3,
      style: { resize: "vertical", minHeight: 90, fontFamily: "inherit" },
      maxLength: 300
    }
  ), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 8, gap: 16 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)" } }, form.building.length, "/300"), /* @__PURE__ */ React.createElement("button", { type: "submit", className: "btn btn-primary", disabled: state === "submitting" }, state === "submitting" ? "Joining\u2026" : "Confirm and join", " ", /* @__PURE__ */ React.createElement("span", { className: "arrow" }, "\u2192"))), err && /* @__PURE__ */ React.createElement("div", { className: "t-small", style: { color: "var(--bone-dim)" } }, "! ", err)));
}
function ThemeToggle() {
  const [theme, setTheme] = useState("dark");
  useEffect(() => {
    const savedTheme = localStorage.getItem("theme");
    const initialTheme = savedTheme || "dark";
    setTheme(initialTheme);
    document.documentElement.setAttribute("data-theme", initialTheme);
  }, []);
  const toggleTheme = () => {
    const newTheme = theme === "dark" ? "light" : "dark";
    setTheme(newTheme);
    localStorage.setItem("theme", newTheme);
    document.documentElement.setAttribute("data-theme", newTheme);
  };
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      onClick: toggleTheme,
      className: "theme-toggle",
      "aria-label": "Toggle theme",
      style: {
        background: "transparent",
        border: "1px solid var(--line)",
        borderRadius: "8px",
        padding: "8px",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "36px",
        height: "36px",
        transition: "all 300ms cubic-bezier(0.16, 1, 0.3, 1)",
        color: "var(--stone-2)"
      },
      onMouseEnter: (e) => {
        e.currentTarget.style.borderColor = "var(--bronze)";
        e.currentTarget.style.color = "var(--bronze)";
      },
      onMouseLeave: (e) => {
        e.currentTarget.style.borderColor = "var(--line)";
        e.currentTarget.style.color = "var(--stone-2)";
      }
    },
    theme === "dark" ? (
      // Sun icon for light mode
      /* @__PURE__ */ React.createElement("svg", { width: "20", height: "20", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2", strokeLinecap: "round", strokeLinejoin: "round" }, /* @__PURE__ */ React.createElement("circle", { cx: "12", cy: "12", r: "5" }), /* @__PURE__ */ React.createElement("line", { x1: "12", y1: "1", x2: "12", y2: "3" }), /* @__PURE__ */ React.createElement("line", { x1: "12", y1: "21", x2: "12", y2: "23" }), /* @__PURE__ */ React.createElement("line", { x1: "4.22", y1: "4.22", x2: "5.64", y2: "5.64" }), /* @__PURE__ */ React.createElement("line", { x1: "18.36", y1: "18.36", x2: "19.78", y2: "19.78" }), /* @__PURE__ */ React.createElement("line", { x1: "1", y1: "12", x2: "3", y2: "12" }), /* @__PURE__ */ React.createElement("line", { x1: "21", y1: "12", x2: "23", y2: "12" }), /* @__PURE__ */ React.createElement("line", { x1: "4.22", y1: "19.78", x2: "5.64", y2: "18.36" }), /* @__PURE__ */ React.createElement("line", { x1: "18.36", y1: "5.64", x2: "19.78", y2: "4.22" }))
    ) : (
      // Moon icon for dark mode
      /* @__PURE__ */ React.createElement("svg", { width: "20", height: "20", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2", strokeLinecap: "round", strokeLinejoin: "round" }, /* @__PURE__ */ React.createElement("path", { d: "M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" }))
    )
  );
}
function Nav({ current }) {
  const [scrolled, setScrolled] = useState(false);
  const [sheet, setSheet] = useState(false);
  const sheetCloseRef = useRef(null);
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 20);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);
  useEffect(() => {
    if (!sheet) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setSheet(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    const focusFrame = requestAnimationFrame(() => {
      var _a;
      return (_a = sheetCloseRef.current) == null ? void 0 : _a.focus();
    });
    return () => {
      cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", closeOnEscape);
      document.body.style.overflow = previousOverflow;
    };
  }, [sheet]);
  const links = [
    { label: "Products", k: "product", children: [
      { href: "/bvx", label: "bvx CLI" },
      { href: "/product", label: "Splice" }
    ] },
    { href: "/benchmarks", label: "Benchmarks", k: "benchmarks" },
    { href: "/for-enterprises", label: "For Enterprises", k: "enterprise" },
    // Docs hidden from nav for now
    { href: "/blog", label: "Research & Blog", k: "blog" }
  ];
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("nav", { className: `nav ${scrolled ? "scrolled" : ""}`, "aria-label": "Primary" }, /* @__PURE__ */ React.createElement("div", { className: "nav-inner" }, /* @__PURE__ */ React.createElement("a", { href: "/", style: { display: "inline-flex", alignItems: "center", color: "var(--fg)" }, "aria-label": "Brevitas Systems \u2014 home" }, /* @__PURE__ */ React.createElement("img", { src: "/assets/b-logo-dark-tight.png", alt: "Brevitas Systems", width: 678, height: 117, className: "nav-logo logo-for-dark", style: { height: 29, width: "auto" } }), /* @__PURE__ */ React.createElement("img", { src: "/assets/b-logo-tight.png", alt: "", "aria-hidden": "true", width: 330, height: 56, className: "nav-logo logo-for-light", style: { height: 29, width: "auto" } })), /* @__PURE__ */ React.createElement("div", { className: "nav-links desktop" }, links.map((l) => l.children ? /* @__PURE__ */ React.createElement("div", { key: l.k, className: "nav-dropdown" }, /* @__PURE__ */ React.createElement("button", { type: "button", className: `nav-link nav-link--drop ${current === l.k ? "active" : ""}`, "aria-haspopup": "true" }, l.label, /* @__PURE__ */ React.createElement("svg", { className: "nav-caret", width: "13", height: "13", viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M6 9l6 6 6-6", stroke: "currentColor", strokeWidth: "2.5", strokeLinecap: "round", strokeLinejoin: "round" }))), /* @__PURE__ */ React.createElement("div", { className: "nav-menu" }, l.children.map((c) => /* @__PURE__ */ React.createElement("a", { key: c.href, href: c.href }, c.label)))) : /* @__PURE__ */ React.createElement("a", { key: l.k, href: l.href, className: `nav-link ${current === l.k ? "active" : ""}` }, l.label))), /* @__PURE__ */ React.createElement(ThemeToggle, null), /* @__PURE__ */ React.createElement(Button, { variant: "primary", href: "https://calendly.com/anish-brevitassystems/30min", target: "_blank", className: "nav-cta" }, "Book a call"), /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "nav-hamburger",
      onClick: () => setSheet(true),
      "aria-label": "Open menu",
      "aria-expanded": sheet,
      "aria-controls": "mobile-navigation"
    },
    /* @__PURE__ */ React.createElement("span", null),
    /* @__PURE__ */ React.createElement("span", null),
    /* @__PURE__ */ React.createElement("span", null)
  ))), sheet && /* @__PURE__ */ React.createElement("div", { id: "mobile-navigation", className: "nav-sheet open", role: "dialog", "aria-modal": "true", "aria-label": "Mobile navigation" }, /* @__PURE__ */ React.createElement("button", { ref: sheetCloseRef, type: "button", className: "nav-sheet-close", onClick: () => setSheet(false), "aria-label": "Close menu" }, "\xD7"), /* @__PURE__ */ React.createElement("div", { className: "nav-sheet-links" }, links.flatMap((l) => l.children ? l.children.map((c) => /* @__PURE__ */ React.createElement("a", { key: c.href, href: c.href }, c.label)) : [/* @__PURE__ */ React.createElement("a", { key: l.k, href: l.href }, l.label)]), /* @__PURE__ */ React.createElement("a", { href: "https://calendly.com/anish-brevitassystems/30min", target: "_blank", rel: "noopener noreferrer", style: { color: "var(--bronze)" } }, "Book a call \u2192"))));
}
const FOOTER_COLS = [
  { title: "Products", links: [["Splice", "/product"], ["bvx CLI", "/bvx"], ["Benchmarks", "/benchmarks"], ["For Enterprises", "/for-enterprises"]] },
  { title: "Company", links: [["Blog", "/blog"], ["Contact", "mailto:james@brevitassystems.com"]] },
  { title: "Resources", links: [["Docs", "/docs"], ["Changelog", "mailto:james@brevitassystems.com"]] },
  { title: "Legal", links: [["Privacy", "/privacy"], ["Terms", "/terms"]] }
];
function Footer() {
  const social = [
    { label: "X", href: "https://x.com/Brevitas_sys", d: "M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" },
    { label: "LinkedIn", href: "https://www.linkedin.com/company/brevitas-ai/", d: "M4.98 3.5C4.98 4.88 3.87 6 2.5 6S0 4.88 0 3.5 1.12 1 2.5 1s2.48 1.12 2.48 2.5zM.5 8h4V24h-4V8zm7.5 0h3.8v2.2h.05c.53-1 1.83-2.2 3.77-2.2 4.03 0 4.78 2.65 4.78 6.1V24h-4v-7.1c0-1.7-.03-3.9-2.38-3.9-2.38 0-2.74 1.86-2.74 3.78V24h-4V8z" },
    { label: "GitHub", href: "https://github.com/Brevitas-ai", d: "M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 016 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58C20.56 22.29 24 17.8 24 12.5 24 5.87 18.63.5 12 .5z" }
  ];
  return /* @__PURE__ */ React.createElement("footer", { className: "footer", style: { position: "relative", overflow: "hidden" } }, /* @__PURE__ */ React.createElement("div", { "aria-hidden": "true", className: "footer-watermark" }, "brevitas"), /* @__PURE__ */ React.createElement("div", { className: "container", style: { position: "relative", zIndex: 1 } }, /* @__PURE__ */ React.createElement("div", { className: "footer-main" }, /* @__PURE__ */ React.createElement("div", { className: "footer-brand" }, /* @__PURE__ */ React.createElement("a", { href: "/", "aria-label": "Brevitas Systems \u2014 home", style: { display: "inline-flex", alignItems: "center" } }, /* @__PURE__ */ React.createElement("img", { src: "/assets/b-logo-dark-tight.png", alt: "Brevitas Systems", width: 678, height: 117, className: "logo-for-dark", style: { height: 26, width: "auto" } }), /* @__PURE__ */ React.createElement("img", { src: "/assets/b-logo-tight.png", alt: "", "aria-hidden": "true", width: 330, height: 56, className: "logo-for-light", style: { height: 26, width: "auto" } })), /* @__PURE__ */ React.createElement("div", { className: "footer-social" }, social.map((s) => /* @__PURE__ */ React.createElement("a", { key: s.label, href: s.href, "aria-label": s.label, target: "_blank", rel: "noopener noreferrer" }, /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 24 24", width: "15", height: "15", fill: "currentColor", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: s.d }))))), /* @__PURE__ */ React.createElement("div", { className: "footer-copy" }, "\xA9 2026 \xB7 All rights reserved")), /* @__PURE__ */ React.createElement("div", { className: "footer-cols" }, FOOTER_COLS.map((col) => /* @__PURE__ */ React.createElement("div", { key: col.title, className: "footer-col" }, /* @__PURE__ */ React.createElement("h4", null, col.title), /* @__PURE__ */ React.createElement("ul", null, col.links.map(([label, href]) => /* @__PURE__ */ React.createElement("li", { key: label }, /* @__PURE__ */ React.createElement("a", { href }, label))))))))));
}
function useFadeUpReveal() {
  useEffect(() => {
    const els = document.querySelectorAll(".fade-up:not(.in)");
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.15, rootMargin: "0px 0px -5% 0px" });
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}
Object.assign(window, {
  useInView,
  useReducedMotion,
  useCountUp,
  useFadeUpReveal,
  LogoMark,
  ArrowRight,
  Overline,
  Button,
  SectionShell,
  StatCard,
  TechniqueCard,
  BenchmarkBadge,
  CodeBlock,
  CodeBlockPy,
  syntaxPython,
  WaitlistInput,
  ThemeToggle,
  Nav,
  Footer,
  InstallCommand
});
