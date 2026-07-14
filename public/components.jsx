// Brevitas Systems — shared UI components
// Exposes: Nav, Footer, Overline, Button, StatCard, TechniqueCard,
// BenchmarkBadge, CodeBlock, SectionShell, WaitlistInput, LogoMark, ArrowRight

const { useState, useEffect, useRef, useCallback } = React;

// ---------------------------------------------------------------------------
// Matrix canvas animation — shared by hero and footer across all pages
// ---------------------------------------------------------------------------
function initMatrixCanvas(canvasId, opts) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return () => {};
  const ctx = canvas.getContext('2d');
  const cursorFollow = !!(opts && opts.cursor);   // opt-in: hero enables the comet, footer does not

  const CELL = 12;
  const CHAR_POOL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$%&*<>{}[]|/\\^~±§ΘΛΞΣΩαβγδ';
  const randChar = () => CHAR_POOL[Math.floor(Math.random() * CHAR_POOL.length)];
  const BASE_OP = 0.06;

  // --- cursor comet (hero only) -------------------------------------------
  // A small DIRECTIONAL glyph set: each lit cell shows the arrow pointing away from the glow center,
  // so the trail reads as a coherent flow (octant 0..7 = E, SE, S, SW, W, NW, N, NE; canvas y is down).
  const DIR_GLYPHS = ['>', '\\', 'v', '/', '<', '\\', '^', '/'];
  function dirGlyph(dx, dy) { let o = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)); if (o < 0) o += 8; return DIR_GLYPHS[o % 8]; }
  const FLOW_POOL = '.-_·:~';   // calm, sparse base field in cursor mode
  const baseChar = () => cursorFollow ? FLOW_POOL[Math.floor(Math.random() * FLOW_POOL.length)] : randChar();

  const CURSOR_R = 74; const CURSOR_AMP = 1.0; const CURSOR_EASE = 0.30; const CURSOR_FOCUS = 1.2;
  const CURSOR_CELL_DECAY = 0.018; const CURSOR_GLOW_UP = 0.18; const CURSOR_GLOW_DOWN = 0.08;
  const CURSOR_IDLE_MS = 650; const CURSOR_DASH_AT = 0.45;   // hold longer, then fade fast; below this glow glyphs resolve to '-'
  const FLOW_FREQ = 0.10; const FLOW_SPEED = 0.009; const FLOW_DEPTH = 0.38;
  const TRAIL_LEN = 34; const TRAIL_TAPER = 0.58; const TRAIL_WOBBLE = 8;   // curving, swaying tail
  const CLICK_MAXR = 168; const CLICK_LIFE_MS = 1000; const CLICK_GROW_MS = 260; const CLICK_AMP = 1.0;
  let clickBursts = [];
  const AMBIENT = cursorFollow ? 0.07 : 1;   // ambient rings dialed down in cursor mode

  const WAVE_THICKNESS = 48; const WAVE_AMP_MIN = 0.45; const WAVE_AMP_MAX = 0.68;
  const WAVE_SPEED_MIN = 1.2; const WAVE_SPEED_MAX = 2.8;
  const WAVE_R_MIN = 120; const WAVE_R_MAX = 280;
  const WAVE_DECAY = 0.028; const WAVE_MUTATE = 0.40; const WAVE_SPAWN = 0.10;

  const POINT_THICKNESS = 32; const POINT_AMP = 0.82;
  const POINT_SPEED_MIN = 0.25; const POINT_SPEED_MAX = 0.50;
  const POINT_R_MIN = 200; const POINT_R_MAX = 380;
  const POINT_DECAY = 0.05;
  const POINT_SPAWN_MS_MIN = 1200; const POINT_SPAWN_MS_MAX = 2800;

  function WaveRipple() {
    this.x = Math.random() * canvas.width; this.y = Math.random() * canvas.height;
    this.radius = 0;
    this.maxRadius = WAVE_R_MIN + Math.random() * (WAVE_R_MAX - WAVE_R_MIN);
    this.speed = WAVE_SPEED_MIN + Math.random() * (WAVE_SPEED_MAX - WAVE_SPEED_MIN);
    this.amplitude = WAVE_AMP_MIN + Math.random() * (WAVE_AMP_MAX - WAVE_AMP_MIN);
  }
  WaveRipple.prototype.intensityAt = function(px, py) {
    const edge = Math.abs(Math.sqrt((px-this.x)**2 + (py-this.y)**2) - this.radius);
    if (edge >= WAVE_THICKNESS) return 0;
    return 0.5 * (1 + Math.cos(Math.PI * edge / WAVE_THICKNESS)) * (1 - this.radius / this.maxRadius) * this.amplitude;
  };
  Object.defineProperty(WaveRipple.prototype, 'dead', { get() { return this.radius >= this.maxRadius; } });

  function PointRipple() {
    this.x = Math.random() * canvas.width; this.y = Math.random() * canvas.height;
    this.radius = 0;
    this.maxRadius = POINT_R_MIN + Math.random() * (POINT_R_MAX - POINT_R_MIN);
    this.speed = POINT_SPEED_MIN + Math.random() * (POINT_SPEED_MAX - POINT_SPEED_MIN);
  }
  PointRipple.prototype.intensityAt = function(px, py) {
    const edge = Math.abs(Math.sqrt((px-this.x)**2 + (py-this.y)**2) - this.radius);
    if (edge >= POINT_THICKNESS) return 0;
    return 0.5 * (1 + Math.cos(Math.PI * edge / POINT_THICKNESS)) * POINT_AMP;
  };
  Object.defineProperty(PointRipple.prototype, 'dead', { get() { return this.radius >= this.maxRadius; } });

  let cols, rows, cells, waveRipples, pointRipples, raf, resizeTimer, spawnTimer;
  let curTX = null, curTY = null, curX = null, curY = null, curActive = false, curGlow = 0, lastMove = 0;
  let trail = [], frameN = 0;   // recent eased head positions (newest first) -> curving comet tail

  function init() {
    canvas.width = canvas.offsetWidth; canvas.height = canvas.offsetHeight;
    cols = Math.ceil(canvas.width / CELL); rows = Math.ceil(canvas.height / CELL);
    waveRipples = []; pointRipples = [];
    cells = Array.from({ length: cols * rows }, () => ({ char: baseChar(), waveOp: 0, pointOp: 0, curOp: 0 }));
    const nInit = Math.round(14 * AMBIENT);
    for (let i = 0; i < nInit; i++) { const r = new WaveRipple(); r.radius = Math.random() * r.maxRadius * 0.7; waveRipples.push(r); }
  }

  function schedulePointRipple() {
    const delay = (POINT_SPAWN_MS_MIN + Math.random() * (POINT_SPAWN_MS_MAX - POINT_SPAWN_MS_MIN)) / AMBIENT;
    spawnTimer = setTimeout(() => { pointRipples.push(new PointRipple()); schedulePointRipple(); }, delay);
  }

  function draw() {
    frameN++;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (Math.random() < WAVE_SPAWN * AMBIENT) waveRipples.push(new WaveRipple());
    for (let i = waveRipples.length - 1; i >= 0; i--) { waveRipples[i].radius += waveRipples[i].speed; if (waveRipples[i].dead) waveRipples.splice(i, 1); }
    for (let i = pointRipples.length - 1; i >= 0; i--) { pointRipples[i].radius += pointRipples[i].speed; if (pointRipples[i].dead) pointRipples.splice(i, 1); }
    const now = performance.now();
    if (cursorFollow) {
      // Ease the head toward the pointer (gentle whip -> curving tail). Lives only while moving:
      // after CURSOR_IDLE_MS still, the glow fades and the tail retracts, then it clears.
      const moving = curActive && curTX != null && (now - lastMove) < CURSOR_IDLE_MS;
      if (moving) {
        if (curX == null) { curX = curTX; curY = curTY; }
        curX += (curTX - curX) * CURSOR_EASE; curY += (curTY - curY) * CURSOR_EASE;
        curGlow = Math.min(1, curGlow + CURSOR_GLOW_UP);
        trail.unshift({ x: curX, y: curY });
        if (trail.length > TRAIL_LEN) trail.pop();
      } else {
        curGlow = Math.max(0, curGlow - CURSOR_GLOW_DOWN);
        if (trail.length) { trail.pop(); trail.pop(); }         // retract fast (two per frame) so the fade is short
        if (curGlow <= 0.01) { trail.length = 0; curX = null; }
      }
      for (let i = clickBursts.length - 1; i >= 0; i--) {        // advance click bursts (filled discs)
        const b = clickBursts[i], age = now - b.t0;
        if (age >= CLICK_LIFE_MS) { clickBursts.splice(i, 1); continue; }
        const grow = Math.min(1, age / CLICK_GROW_MS);
        b.radius = CLICK_MAXR * (1 - Math.pow(1 - grow, 3));
        b.alpha = (1 - age / CLICK_LIFE_MS) * CLICK_AMP;
      }
    }
    ctx.font = `${CELL - 2}px "JetBrains Mono","Courier New",monospace`;
    for (let row = 0; row < rows; row++) {
      const py = row * CELL + CELL;
      for (let col = 0; col < cols; col++) {
        const px = col * CELL + CELL * 0.5;
        const cell = cells[row * cols + col];
        let wTotal = 0, wPeak = 0;
        for (const r of waveRipples) { const i = r.intensityAt(px, py); if (i === 0) continue; wTotal = Math.min(1, wTotal + i); if (i > wPeak) wPeak = i; }
        if (wTotal > 0.03) { cell.waveOp = Math.max(cell.waveOp, wTotal); if (wPeak > WAVE_MUTATE && Math.random() < 0.35) cell.char = randChar(); }
        else { cell.waveOp = Math.max(0, cell.waveOp - WAVE_DECAY); }
        let pTotal = 0;
        for (const r of pointRipples) { const i = r.intensityAt(px, py); if (i > 0) pTotal = Math.min(1, pTotal + i); }
        if (pTotal > 0.02) { cell.pointOp = Math.max(cell.pointOp, pTotal); if (Math.random() < 0.55) cell.char = randChar(); }
        else { cell.pointOp = Math.max(0, cell.pointOp - POINT_DECAY); }
        // Cursor comet: decay first (so idle/left-behind cells go dark), then relight along the trail
        // chain (tapering, swaying), modulated by outward FLOW bands. Arrows while bright; '-' on fade.
        if (cursorFollow) {
          if (cell.curOp > 0) cell.curOp = Math.max(0, cell.curOp - CURSOR_CELL_DECAY);
          if (curGlow > 0.01 && trail.length) {
            let best = 0, bdx = 0, bdy = 0, bcd = 0;
            for (let k = 0; k < trail.length; k++) {
              const tp = trail[k], sway = k / TRAIL_LEN;
              const wob = Math.sin(now * 0.004 + k * 0.55) * TRAIL_WOBBLE * sway;
              const dx = px - (tp.x + wob), dy = py - (tp.y - wob * 0.6);
              const rk = CURSOR_R * (1 - TRAIL_TAPER * sway);
              if (dx < -rk || dx > rk || dy < -rk || dy > rk) continue;
              const cd = Math.sqrt(dx * dx + dy * dy);
              if (cd >= rk) continue;
              const falloff = 0.5 * (1 + Math.cos(Math.PI * cd / rk));
              const g = Math.pow(falloff, CURSOR_FOCUS) * (1 - sway * 0.5);
              if (g > best) { best = g; bdx = dx; bdy = dy; bcd = cd; }
            }
            if (best > 0) {
              const flow = 1 - FLOW_DEPTH * 0.5 * (1 - Math.cos(bcd * FLOW_FREQ - now * FLOW_SPEED));
              const lit = best * CURSOR_AMP * curGlow * flow;
              if (lit > cell.curOp) cell.curOp = lit;
              cell.char = (best > 0.06 && curGlow > CURSOR_DASH_AT) ? dirGlyph(bdx, bdy) : '-';
            }
          }
          if (cell.curOp > 0 && curGlow <= CURSOR_DASH_AT) cell.char = '-';
        }
        let clkOp = 0;
        for (const b of clickBursts) {
          const bdx = px - b.x, bdy = py - b.y, bd = Math.sqrt(bdx * bdx + bdy * bdy);
          if (bd < b.radius) {
            const edge = bd > b.radius * 0.72 ? (b.radius - bd) / (b.radius * 0.28) : 1;
            const c = edge * b.alpha;
            if (c > clkOp) clkOp = c;
            if (c > 0.15) cell.char = dirGlyph(bdx, bdy);
          }
        }
        const glowOp = cell.pointOp + cell.curOp + clkOp;
        const finalOp = Math.min(1, BASE_OP + cell.waveOp + glowOp);
        if (finalOp < 0.02) continue;
        ctx.fillStyle = `rgba(255,255,255,${finalOp.toFixed(3)})`;
        if (glowOp > 0.10) { ctx.shadowColor = 'rgba(255,255,255,0.95)'; ctx.shadowBlur = 13; }
        ctx.fillText(cell.char, col * CELL, py);
        if (glowOp > 0.10) ctx.shadowBlur = 0;
      }
    }
  }

  // Perf: pause when off-screen or tab hidden, honor reduced-motion. Cursor mode runs at 60fps so
  // the comet feels immediate; ambient-only canvases (footer) stay at 30fps.
  const FRAME_MS = 1000 / (cursorFollow ? 60 : 30);
  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  let lastFrame = 0, inView = true, pageVisible = !document.hidden;
  function loop(ts) {
    raf = requestAnimationFrame(loop);
    if (ts - lastFrame < FRAME_MS) return;
    lastFrame = ts;
    draw();
  }
  function start() { if (raf == null && !reduceMotion) { lastFrame = 0; raf = requestAnimationFrame(loop); } }
  function stop() { if (raf != null) { cancelAnimationFrame(raf); raf = null; } }
  function sync() { (inView && pageVisible) ? start() : stop(); }

  const io = new IntersectionObserver(([e]) => { inView = e.isIntersecting; sync(); }, { threshold: 0 });
  const onVisibility = () => { pageVisible = !document.hidden; sync(); };
  const onResize = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { stop(); init(); reduceMotion ? draw() : start(); }, 150); };

  // Cursor tracking (hero only): canvas is pointer-events:none, so map the window pointer into it.
  const onMouseMove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) { curActive = false; return; }
    curTX = x * (canvas.width / rect.width); curTY = y * (canvas.height / rect.height);
    curActive = true; lastMove = performance.now();
  };
  const onMouseOut = (e) => { if (!e.relatedTarget && !e.toElement) { curActive = false; curX = null; } };
  const onMouseDown = (e) => {
    if (e.button !== 0) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
    clickBursts.push({ x: x * (canvas.width / rect.width), y: y * (canvas.height / rect.height), t0: performance.now(), radius: 0, alpha: 1 });
    if (clickBursts.length > 6) clickBursts.shift();
  };

  window.addEventListener('resize', onResize);
  document.addEventListener('visibilitychange', onVisibility);
  if (cursorFollow) {
    window.addEventListener('mousemove', onMouseMove, { passive: true });
    document.addEventListener('mouseout', onMouseOut);
    window.addEventListener('mousedown', onMouseDown, { passive: true });
  }
  io.observe(canvas);
  init();
  if (!cursorFollow) pointRipples.push(new PointRipple());   // no big opening ring in cursor mode
  if (!reduceMotion) { schedulePointRipple(); start(); } else { draw(); }

  return () => { stop(); io.disconnect(); clearTimeout(spawnTimer); clearTimeout(resizeTimer); window.removeEventListener('resize', onResize); document.removeEventListener('visibilitychange', onVisibility); if (cursorFollow) { window.removeEventListener('mousemove', onMouseMove); document.removeEventListener('mouseout', onMouseOut); window.removeEventListener('mousedown', onMouseDown); } };
}

// --- utility hooks ---
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
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    setR(mq.matches);
    const h = (e) => setR(e.matches);
    mq.addEventListener('change', h);
    return () => mq.removeEventListener('change', h);
  }, []);
  return r;
}

function useCountUp(target, { duration = 800, start = 0, trigger = true } = {}) {
  const [val, setVal] = useState(start);
  useEffect(() => {
    if (!trigger) return;
    let raf;
    const t0 = performance.now();
    const ease = (t) => t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2, 2)/2;
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

// --- atoms ---
function LogoMark({ size = 28, color }) {
  const s = size;
  const style = { color: color || 'currentColor' };
  return (
    <svg width={s} height={s} viewBox="0 0 28 28" style={style} aria-hidden="true">
      <rect x="1" y="1" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="1.5"/>
      <line x1="14" y1="4" x2="14" y2="24" stroke="currentColor" strokeWidth="1.5"/>
      <line x1="4" y1="14" x2="24" y2="14" stroke="currentColor" strokeWidth="1.5"/>
    </svg>
  );
}

function ArrowRight({ size = 14 }) {
  return <span className="arrow" aria-hidden="true">→</span>;
}

function Overline({ children, dot = false, className = '' }) {
  return (
    <div className={`overline t-overline ${className}`}>
      {dot && <span className="overline-dot" />}
      <span>{children}</span>
    </div>
  );
}

function Button({ variant = 'primary', href, onClick, children, arrow = true, className = '' }) {
  const cls = `btn btn-${variant} ${className}`;
  const content = (
    <>
      <span>{children}</span>
      {arrow && <span className="arrow" aria-hidden="true">→</span>}
    </>
  );
  if (href) return <a href={href} className={cls} onClick={onClick}>{content}</a>;
  return <button className={cls} onClick={onClick} type="button">{content}</button>;
}

function SectionShell({ overline, overlineDot, rule = false, children, className = '', id, tight = false }) {
  return (
    <section id={id} className={`section ${tight ? 'section--tight' : ''} ${className}`}>
      <div className="container">
        {rule && <hr className="rule" style={{ marginBottom: 48 }} />}
        {overline && <Overline dot={overlineDot}>{overline}</Overline>}
        <div style={{ marginTop: overline ? 32 : 0 }}>
          {children}
        </div>
      </div>
    </section>
  );
}

function StatCard({ value, label, sub, variant = 'default', delta, onInView = true }) {
  const [ref, inView] = useInView();
  const numericTarget = typeof value === 'number' ? value : parseFloat(String(value).replace(/[^-0-9.]/g, ''));
  const suffix = typeof value === 'string' ? (value.match(/%$/) ? '%' : '') : '%';
  const prefix = typeof value === 'string' && value.startsWith('–') ? '–' : '';
  const count = useCountUp(numericTarget, { duration: 900, trigger: inView });
  const display = isNaN(numericTarget) ? value : `${prefix}${count.toFixed(1).replace(/\.0$/, '')}${suffix}`;

  if (variant === 'inline') {
    return (
      <div ref={ref} className="stat-inline" style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 140 }}>
        <div className="serif tabular" style={{ fontSize: 'clamp(32px, 3.6vw, 44px)', fontWeight: 300, lineHeight: 1, color: 'var(--fg)', letterSpacing: '-0.01em' }}>
          {display}
        </div>
        <div className="t-mono" style={{ color: 'var(--stone-2)', marginTop: 6 }}>{label}</div>
      </div>
    );
  }
  const isEmph = variant === 'emphasis';
  return (
    <div ref={ref} className="card" style={{ padding: isEmph ? '40px 32px' : 28 }}>
      <div className="serif tabular" style={{
        fontSize: isEmph ? 'clamp(64px, 7vw, 96px)' : 'clamp(40px, 4.5vw, 56px)',
        fontWeight: 300,
        lineHeight: 1,
        letterSpacing: '-0.02em',
        color: 'var(--fg)',
      }}>
        {display}
      </div>
      <hr className="rule" style={{ margin: '20px 0 16px', width: 40, borderColor: 'var(--line)' }} />
      <div className="t-mono" style={{ color: 'var(--fg-dim)' }}>{label}</div>
      {sub && <div className="t-mono" style={{ color: 'var(--stone)', marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function TechniqueCard({ index, title, body, demoLink }) {
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 16, minHeight: 260 }}>
      <div className="t-mono" style={{ color: 'var(--stone)' }}>{String(index).padStart(2, '0')}</div>
      <div className="serif" style={{ fontSize: 24, fontWeight: 400, lineHeight: 1.22, letterSpacing: '-0.01em' }}>
        {title}
      </div>
      <div className="t-body" style={{ color: 'var(--bone-dim)', flex: 1 }}>{body}</div>
      {demoLink && (
        <a href={demoLink} className="t-mono link" style={{ color: 'var(--bronze)', marginTop: 8 }}>
          see it →
        </a>
      )}
    </div>
  );
}

function BenchmarkBadge({ letter, name, venue }) {
  return (
    <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '20px 24px', minWidth: 220 }}>
      <div style={{
        width: 44, height: 44,
        border: '1.5px solid var(--stone-2)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontFamily: 'Newsreader, serif', fontWeight: 400, fontSize: 22,
        color: 'var(--fg)',
      }}>
        {letter}
      </div>
      <div>
        <div style={{ fontWeight: 500, fontSize: 15, color: 'var(--fg)' }}>{name}</div>
        <div className="t-mono" style={{ color: 'var(--stone-2)', fontSize: 12 }}>{venue}</div>
      </div>
    </div>
  );
}

function CodeBlock({ children, copyable = false, language = 'python', filename }) {
  const [copied, setCopied] = useState(false);
  const doCopy = () => {
    try {
      const text = typeof children === 'string' ? children : (children?.props?.children ?? '');
      navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {}
  };
  return (
    <div style={{
      background: 'var(--ink-2)',
      border: '1px solid var(--line)',
      borderRadius: 4,
      position: 'relative',
      overflow: 'hidden',
    }}>
      {filename && (
        <div className="t-mono" style={{
          padding: '10px 20px',
          borderBottom: '1px solid var(--line)',
          color: 'var(--stone-2)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span>{filename}</span>
          {copyable && (
            <button onClick={doCopy} className="t-mono" style={{ background: 'transparent', border: 0, color: copied ? 'var(--signal)' : 'var(--stone-2)', cursor: 'pointer', padding: 0 }}>
              {copied ? '✓ copied' : '⧉ copy'}
            </button>
          )}
        </div>
      )}
      <pre style={{
        margin: 0,
        padding: 20,
        fontFamily: 'JetBrains Mono, ui-monospace, monospace',
        fontSize: 14,
        lineHeight: 1.6,
        color: 'var(--bone-dim)',
        overflowX: 'auto',
      }}>
        <code>{children}</code>
      </pre>
    </div>
  );
}

// Tiny syntax highlighter — minimalist 4-color theme
function syntaxPython(src) {
  const lines = src.split('\n');
  return lines.map((line, i) => {
    // split comment off
    let codePart = line, commentPart = '';
    const hashIdx = (() => {
      let inStr = null;
      for (let j = 0; j < line.length; j++) {
        const c = line[j];
        if (inStr) { if (c === inStr && line[j-1] !== '\\') inStr = null; }
        else if (c === '"' || c === "'") inStr = c;
        else if (c === '#') return j;
      }
      return -1;
    })();
    if (hashIdx >= 0) { codePart = line.slice(0, hashIdx); commentPart = line.slice(hashIdx); }

    const parts = [];
    const re = /(\s+)|("[^"]*"|'[^']*')|\b(import|from|def|return|class|if|else|for|in|as|with|pass|None|True|False|await|async)\b|(\b\d[\d_.]*\b)|([A-Za-z_][A-Za-z0-9_]*)|(.)/g;
    let m, idx = 0;
    while ((m = re.exec(codePart)) !== null) {
      if (m[1]) parts.push(<span key={idx++}>{m[1]}</span>);
      else if (m[2]) parts.push(<span key={idx++} style={{ color: 'var(--signal)' }}>{m[2]}</span>);
      else if (m[3]) parts.push(<span key={idx++} style={{ color: 'var(--bone)', fontWeight: 500 }}>{m[3]}</span>);
      else if (m[4]) parts.push(<span key={idx++} style={{ color: 'var(--stone-2)' }}>{m[4]}</span>);
      else if (m[5]) parts.push(<span key={idx++} style={{ color: 'var(--bone-dim)' }}>{m[5]}</span>);
      else if (m[6]) parts.push(<span key={idx++}>{m[6]}</span>);
    }
    if (commentPart) parts.push(<span key={'c'+i} style={{ color: 'var(--stone)' }}>{commentPart}</span>);
    return <div key={i}>{parts.length ? parts : '\u00A0'}</div>;
  });
}

function CodeBlockPy({ source, filename, copyable = true }) {
  return (
    <CodeBlock filename={filename} copyable={copyable}>
      <>{syntaxPython(source)}</>
    </CodeBlock>
  );
}

// One-line install command — copyable mono pill for hero / CTA sections
function InstallCommand({ command }) {
  const [copied, setCopied] = useState(false);
  const doCopy = () => {
    try {
      navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {}
  };
  return (
    <button
      onClick={doCopy}
      className="t-mono"
      title="Copy install command"
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 18,
        maxWidth: '100%',
        // Fixed dark translucent fill so white hero text stays legible over the
        // photo in both light and dark mode (don't use theme --ink-2 here).
        background: 'rgba(0,0,0,0.5)',
        backdropFilter: 'blur(6px)',
        WebkitBackdropFilter: 'blur(6px)',
        border: '1px solid rgba(255,255,255,0.22)',
        borderRadius: 10,
        padding: '16px 22px',
        color: '#fff',
        fontSize: 17,
        cursor: 'pointer',
        textAlign: 'left',
      }}
    >
      <span aria-hidden="true" style={{ color: 'var(--bronze)', userSelect: 'none' }}>$</span>
      <span style={{ overflowX: 'auto', whiteSpace: 'nowrap' }}>{command}</span>
      <span style={{ color: copied ? 'var(--signal)' : 'var(--stone-2)', userSelect: 'none', flexShrink: 0 }}>
        {copied ? '✓ copied' : '⧉ copy'}
      </span>
    </button>
  );
}

// --- Waitlist ---
function WaitlistInput({ variant = 'inline', source = 'unknown' }) {
  const [email, setEmail] = useState('');
  const [expanded, setExpanded] = useState(variant === 'full');
  const [state, setState] = useState('idle'); // idle | submitting | success | error
  const [err, setErr] = useState('');
  const [form, setForm] = useState({ name: '', company: '', role: '', building: '' });

  const emailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());

  const submitEmail = (e) => {
    e?.preventDefault?.();
    if (!emailValid) { setErr("Hmm, that email doesn't look right."); return; }
    setErr('');
    setExpanded(true);
  };

  const submitAll = async (e) => {
    e?.preventDefault?.();
    if (!emailValid) { setErr("Hmm, that email doesn't look right."); return; }
    if (form.name.trim().length < 2) { setErr('Need your name to say hello.'); return; }
    if (form.company.trim().length < 2) { setErr('What company is this for?'); return; }
    if (!form.role) { setErr('Pick a role so we can triage.'); return; }
    if (form.building.trim().length < 20) { setErr('A sentence or two helps us triage — what\'s the multi-agent use case?'); return; }
    setErr('');
    setState('submitting');

    try {
      // Call the backend API
      const response = await fetch('/api/waitlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: email.trim(),
          name: form.name.trim(),
          company: form.company.trim(),
          role: form.role,
          notes: form.building.trim(),
        }),
      });

      const data = await response.json();

      if (response.ok && data.success) {
        setState('success');
      } else {
        setErr(data.error || 'Failed to join waitlist. Please try again.');
        setState('idle');
      }
    } catch (error) {
      console.error('Waitlist submission error:', error);
      setErr('Connection error. Please try again.');
      setState('idle');
    }
  };

  if (state === 'success') {
    return (
      <div style={{
        border: '1px solid var(--line)',
        padding: variant === 'full' ? '40px 32px' : '28px 24px',
        borderRadius: 4,
        background: 'var(--ink-2)',
        maxWidth: variant === 'full' ? 720 : 560,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
          <span style={{ color: 'var(--signal)', fontSize: 24 }}>✓</span>
          <div className="serif" style={{ fontSize: 28, fontWeight: 300, letterSpacing: '-0.01em' }}>You're on the list.</div>
        </div>
        <div className="t-body">
          We'll email at <span className="mono" style={{ color: 'var(--fg)' }}>{email.split('@')[1]}</span> when we have room to talk. In the meantime, watch for a monthly note — no more, usually less.
        </div>
      </div>
    );
  }

  if (variant === 'footer-small') {
    return (
      <form onSubmit={submitEmail} style={{ display: 'flex', gap: 6, alignItems: 'stretch' }}>
        <input
          type="email"
          className="input"
          placeholder="name@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={{ padding: '10px 14px', fontSize: 14 }}
        />
        <button type="submit" className="btn btn-primary" style={{ padding: '10px 16px', fontSize: 14 }}>
          Join <span className="arrow">→</span>
        </button>
      </form>
    );
  }

  return (
    <form onSubmit={expanded ? submitAll : submitEmail} style={{ maxWidth: variant === 'full' ? 720 : 560 }}>
      {!expanded && (
        <>
          <div className="waitlist-row">
            <input
              type="email"
              className="input"
              placeholder="name@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-label="Work email"
            />
            <button type="submit" className="btn btn-primary">
              Join now <span className="arrow">→</span>
            </button>
          </div>
          {err && <div className="t-small" style={{ marginTop: 10, color: 'var(--bone-dim)' }}>! {err}</div>}
        </>
      )}
      {expanded && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div className="waitlist-row">
            <input
              type="email"
              className="input"
              placeholder="name@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-label="Work email"
            />
            <span style={{
              display: 'inline-flex', alignItems: 'center', padding: '0 14px',
              border: '1px solid var(--line)', borderRadius: 2,
              color: emailValid ? 'var(--signal)' : 'var(--stone)',
            }}>{emailValid ? '✓' : ''}</span>
          </div>
          <input
            className="input"
            placeholder="Your name"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <input
            className="input"
            placeholder="Company"
            value={form.company}
            onChange={(e) => setForm({ ...form, company: e.target.value })}
          />
          <select
            className="input"
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
            style={{ color: form.role ? 'var(--fg)' : 'var(--stone)' }}
          >
            <option value="">Role</option>
            <option value="Founder / CEO">Founder / CEO</option>
            <option value="CTO / Head of Engineering">CTO / Head of Engineering</option>
            <option value="Engineering IC">Engineering IC</option>
            <option value="Product / Platform Lead">Product / Platform Lead</option>
            <option value="Investor">Investor</option>
            <option value="Researcher">Researcher</option>
            <option value="Other">Other</option>
          </select>
          <textarea
            className="input"
            placeholder={'e.g., "Three-agent research pipeline on GPT-4o and Claude 4.6 — ~10M tokens/month, cost the main constraint."'}
            value={form.building}
            onChange={(e) => setForm({ ...form, building: e.target.value })}
            rows={3}
            style={{ resize: 'vertical', minHeight: 90, fontFamily: 'inherit' }}
            maxLength={300}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8, gap: 16 }}>
            <div className="t-mono" style={{ color: 'var(--stone)' }}>
              {form.building.length}/300
            </div>
            <button type="submit" className="btn btn-primary" disabled={state === 'submitting'}>
              {state === 'submitting' ? 'Joining…' : 'Confirm and join'} <span className="arrow">→</span>
            </button>
          </div>
          {err && <div className="t-small" style={{ color: 'var(--bone-dim)' }}>! {err}</div>}
        </div>
      )}
    </form>
  );
}

// --- Theme Toggle ---
function ThemeToggle() {
  const [theme, setTheme] = useState('dark');

  useEffect(() => {
    // Check localStorage and system preference on mount
    const savedTheme = localStorage.getItem('theme');
    const initialTheme = savedTheme || 'dark';

    setTheme(initialTheme);
    document.documentElement.setAttribute('data-theme', initialTheme);
  }, []);

  const toggleTheme = () => {
    const newTheme = theme === 'dark' ? 'light' : 'dark';
    setTheme(newTheme);
    localStorage.setItem('theme', newTheme);
    document.documentElement.setAttribute('data-theme', newTheme);
  };

  return (
    <button
      onClick={toggleTheme}
      className="theme-toggle"
      aria-label="Toggle theme"
      style={{
        background: 'transparent',
        border: '1px solid var(--line)',
        borderRadius: '8px',
        padding: '8px',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: '36px',
        height: '36px',
        transition: 'all 300ms cubic-bezier(0.16, 1, 0.3, 1)',
        color: 'var(--stone-2)',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--bronze)';
        e.currentTarget.style.color = 'var(--bronze)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--line)';
        e.currentTarget.style.color = 'var(--stone-2)';
      }}
    >
      {theme === 'dark' ? (
        // Sun icon for light mode
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5"/>
          <line x1="12" y1="1" x2="12" y2="3"/>
          <line x1="12" y1="21" x2="12" y2="23"/>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
          <line x1="1" y1="12" x2="3" y2="12"/>
          <line x1="21" y1="12" x2="23" y2="12"/>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
        </svg>
      ) : (
        // Moon icon for dark mode
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
      )}
    </button>
  );
}

// --- Nav / Footer ---
function Nav({ current }) {
  const [scrolled, setScrolled] = useState(false);
  const [sheet, setSheet] = useState(false);
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 20);
    on();
    window.addEventListener('scroll', on, { passive: true });
    return () => window.removeEventListener('scroll', on);
  }, []);

  const links = [
    { href: '/product', label: 'Product', k: 'product' },
    { href: '/benchmarks', label: 'Benchmarks', k: 'benchmarks' },
    { href: '/pricing', label: 'Pricing', k: 'pricing' },
    // Docs hidden from nav for now
    { href: '/blog', label: 'Blog', k: 'blog' },
  ];
  return (
    <>
      <nav className={`nav ${scrolled ? 'scrolled' : ''}`} aria-label="Primary">
        <div className="nav-inner">
          <a href="/" style={{ display: 'inline-flex', alignItems: 'center', color: 'var(--fg)' }} aria-label="Brevitas Systems — home">
            <img src="/assets/b-logo-dark-tight.png" alt="Brevitas Systems" className="nav-logo logo-for-dark" style={{ height: 29, width: 'auto' }} />
            <img src="/assets/b-logo-tight.png" alt="" aria-hidden="true" className="nav-logo logo-for-light" style={{ height: 29, width: 'auto' }} />
          </a>
          <div className="nav-links desktop">
            {links.map(l => (
              <a key={l.k} href={l.href} className={`nav-link ${current === l.k ? 'active' : ''}`}>{l.label}</a>
            ))}
          </div>
          <ThemeToggle />
          <Button variant="primary" href="/login" className="nav-cta">Sign up</Button>
          <button className="nav-hamburger" onClick={() => setSheet(true)} aria-label="Menu">
            <span/><span/><span/>
          </button>
        </div>
      </nav>
      {sheet && (
        <div className="nav-sheet open" role="dialog" aria-modal="true">
          <button className="nav-sheet-close" onClick={() => setSheet(false)} aria-label="Close">×</button>
          <div className="nav-sheet-links">
            {links.map(l => <a key={l.k} href={l.href}>{l.label}</a>)}
            <a href="/login" style={{ color: 'var(--bronze)' }}>Sign up →</a>
          </div>
        </div>
      )}
    </>
  );
}

const FOOTER_COLS = [
  { title: 'Product', links: [['Product', '/product'], ['Benchmarks', '/benchmarks'], ['Pricing', '/pricing']] },
  { title: 'Company', links: [['Blog', '/blog'], ['Contact', 'mailto:james@brevitassystems.com']] },
  { title: 'Resources', links: [['Docs', 'mailto:james@brevitassystems.com'], ['Changelog', 'mailto:james@brevitassystems.com']] },
  { title: 'Legal', links: [['Privacy', '/privacy'], ['Terms', '/terms']] },
];

function Footer() {
  const social = [
    { label: 'X', href: 'https://x.com/Brevitas_sys', d: 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z' },
    { label: 'LinkedIn', href: 'https://www.linkedin.com/company/brevitas-ai/', d: 'M4.98 3.5C4.98 4.88 3.87 6 2.5 6S0 4.88 0 3.5 1.12 1 2.5 1s2.48 1.12 2.48 2.5zM.5 8h4V24h-4V8zm7.5 0h3.8v2.2h.05c.53-1 1.83-2.2 3.77-2.2 4.03 0 4.78 2.65 4.78 6.1V24h-4v-7.1c0-1.7-.03-3.9-2.38-3.9-2.38 0-2.74 1.86-2.74 3.78V24h-4V8z' },
    { label: 'GitHub', href: 'https://github.com/Brevitas-ai', d: 'M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 016 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58C20.56 22.29 24 17.8 24 12.5 24 5.87 18.63.5 12 .5z' },
  ];
  return (
    <footer className="footer" style={{ position: 'relative', overflow: 'hidden' }}>
      <div aria-hidden="true" className="footer-watermark">brevitas</div>
      <div className="container" style={{ position: 'relative', zIndex: 1 }}>
        <div className="footer-main">
          <div className="footer-brand">
            <a href="/" aria-label="Brevitas Systems — home" style={{ display: 'inline-flex', alignItems: 'center' }}>
              <img src="/assets/b-logo-dark-tight.png" alt="Brevitas Systems" className="logo-for-dark" style={{ height: 26, width: 'auto' }} />
              <img src="/assets/b-logo-tight.png" alt="" aria-hidden="true" className="logo-for-light" style={{ height: 26, width: 'auto' }} />
            </a>
            <div className="footer-social">
              {social.map(s => (
                <a key={s.label} href={s.href} aria-label={s.label} target="_blank" rel="noopener noreferrer">
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><path d={s.d} /></svg>
                </a>
              ))}
            </div>
            <div className="footer-copy">© 2026 · All rights reserved</div>
          </div>
          <div className="footer-cols">
            {FOOTER_COLS.map(col => (
              <div key={col.title} className="footer-col">
                <h4>{col.title}</h4>
                <ul>
                  {col.links.map(([label, href]) => <li key={label}><a href={href}>{label}</a></li>)}
                </ul>
              </div>
            ))}
          </div>
        </div>
      </div>
    </footer>
  );
}

// Scroll-reveal controller — adds .in class to .fade-up when in view
function useFadeUpReveal() {
  useEffect(() => {
    const els = document.querySelectorAll('.fade-up:not(.in)');
    const io = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.15, rootMargin: '0px 0px -5% 0px' });
    els.forEach(el => io.observe(el));
    return () => io.disconnect();
  }, []);
}

Object.assign(window, {
  useInView, useReducedMotion, useCountUp, useFadeUpReveal,
  LogoMark, ArrowRight, Overline, Button, SectionShell,
  StatCard, TechniqueCard, BenchmarkBadge,
  CodeBlock, CodeBlockPy, syntaxPython,
  WaitlistInput, ThemeToggle, Nav, Footer,
});
