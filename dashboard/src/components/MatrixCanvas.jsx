import { useEffect, useRef } from 'react'

// Ported verbatim from the marketing site's shared hero/footer canvas
// (public/components.jsx :: initMatrixCanvas) so the dashboard sidebar shows the
// exact same ASCII glyph-field animation. Renders white glyphs, so it sits directly
// on the blue sidebar. `cursor` enables the hero's pointer comet + click bursts;
// left off, it's the calmer ambient (footer) field. Honors reduced-motion and pauses
// off-screen / when the tab is hidden.
function runMatrixCanvas(canvas, opts) {
  const ctx = canvas.getContext('2d')
  const cursorFollow = !!(opts && opts.cursor)

  const CELL = 12
  const CHAR_POOL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$%&*<>{}[]|/\\^~±§ΘΛΞΣΩαβγδ'
  const randChar = () => CHAR_POOL[Math.floor(Math.random() * CHAR_POOL.length)]
  const BASE_OP = 0.06

  const DIR_GLYPHS = ['>', '\\', 'v', '/', '<', '\\', '^', '/']
  function dirGlyph(dx, dy) { let o = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)); if (o < 0) o += 8; return DIR_GLYPHS[o % 8] }
  const RING_GLYPHS = ['-', '\\', '|', '/', '-', '\\', '|', '/']
  function ringGlyph(dx, dy) { let o = Math.round((Math.atan2(dy, dx) + Math.PI / 2) / (Math.PI / 4)); if (o < 0) o += 8; return RING_GLYPHS[o % 8] }
  const FLOW_POOL = '.-_·:~'
  const baseChar = () => cursorFollow ? FLOW_POOL[Math.floor(Math.random() * FLOW_POOL.length)] : randChar()

  const CURSOR_R = 74; const CURSOR_AMP = 1.0; const CURSOR_EASE = 0.30; const CURSOR_FOCUS = 1.2
  const CURSOR_CELL_DECAY = 0.018; const CURSOR_GLOW_UP = 0.18; const CURSOR_GLOW_DOWN = 0.08
  const CURSOR_IDLE_MS = 650; const CURSOR_DASH_AT = 0.45
  const FLOW_FREQ = 0.10; const FLOW_SPEED = 0.009; const FLOW_DEPTH = 0.38
  const TRAIL_LEN = 34; const TRAIL_TAPER = 0.58; const TRAIL_WOBBLE = 8
  const CLICK_MAXR = 228; const CLICK_LIFE_MS = 1300; const CLICK_AMP = 1.0
  const CLICK_RING_COUNT = 3; const CLICK_RING_GAP = 28; const CLICK_RING_THICKNESS = 16
  const CLICK_CENTER_LIFE_MS = 340; const CLICK_CENTER_MAXR = 58
  let clickBursts = []
  const AMBIENT = cursorFollow ? 0.07 : 1

  const WAVE_THICKNESS = 48; const WAVE_AMP_MIN = 0.45; const WAVE_AMP_MAX = 0.68
  const WAVE_SPEED_MIN = 1.2; const WAVE_SPEED_MAX = 2.8
  const WAVE_R_MIN = 120; const WAVE_R_MAX = 280
  const WAVE_DECAY = 0.028; const WAVE_MUTATE = 0.40; const WAVE_SPAWN = 0.10

  const POINT_THICKNESS = 32; const POINT_AMP = 0.82
  const POINT_SPEED_MIN = 0.25; const POINT_SPEED_MAX = 0.50
  const POINT_R_MIN = 200; const POINT_R_MAX = 380
  const POINT_DECAY = 0.05
  const POINT_SPAWN_MS_MIN = 1200; const POINT_SPAWN_MS_MAX = 2800

  function WaveRipple() {
    this.x = Math.random() * canvas.width; this.y = Math.random() * canvas.height
    this.radius = 0
    this.maxRadius = WAVE_R_MIN + Math.random() * (WAVE_R_MAX - WAVE_R_MIN)
    this.speed = WAVE_SPEED_MIN + Math.random() * (WAVE_SPEED_MAX - WAVE_SPEED_MIN)
    this.amplitude = WAVE_AMP_MIN + Math.random() * (WAVE_AMP_MAX - WAVE_AMP_MIN)
  }
  WaveRipple.prototype.intensityAt = function (px, py) {
    const edge = Math.abs(Math.sqrt((px - this.x) ** 2 + (py - this.y) ** 2) - this.radius)
    if (edge >= WAVE_THICKNESS) return 0
    return 0.5 * (1 + Math.cos(Math.PI * edge / WAVE_THICKNESS)) * (1 - this.radius / this.maxRadius) * this.amplitude
  }
  Object.defineProperty(WaveRipple.prototype, 'dead', { get() { return this.radius >= this.maxRadius } })

  function PointRipple() {
    this.x = Math.random() * canvas.width; this.y = Math.random() * canvas.height
    this.radius = 0
    this.maxRadius = POINT_R_MIN + Math.random() * (POINT_R_MAX - POINT_R_MIN)
    this.speed = POINT_SPEED_MIN + Math.random() * (POINT_SPEED_MAX - POINT_SPEED_MIN)
  }
  PointRipple.prototype.intensityAt = function (px, py) {
    const edge = Math.abs(Math.sqrt((px - this.x) ** 2 + (py - this.y) ** 2) - this.radius)
    if (edge >= POINT_THICKNESS) return 0
    return 0.5 * (1 + Math.cos(Math.PI * edge / POINT_THICKNESS)) * POINT_AMP
  }
  Object.defineProperty(PointRipple.prototype, 'dead', { get() { return this.radius >= this.maxRadius } })

  let cols, rows, cells, waveRipples, pointRipples, raf, resizeTimer, spawnTimer
  let curTX = null, curTY = null, curX = null, curY = null, curActive = false, curGlow = 0, lastMove = 0
  let trail = [], frameN = 0

  function init() {
    canvas.width = canvas.offsetWidth; canvas.height = canvas.offsetHeight
    cols = Math.ceil(canvas.width / CELL); rows = Math.ceil(canvas.height / CELL)
    waveRipples = []; pointRipples = []
    cells = Array.from({ length: cols * rows }, () => ({ char: baseChar(), waveOp: 0, pointOp: 0, curOp: 0 }))
    const nInit = Math.round(14 * AMBIENT)
    for (let i = 0; i < nInit; i++) { const r = new WaveRipple(); r.radius = Math.random() * r.maxRadius * 0.7; waveRipples.push(r) }
  }

  function schedulePointRipple() {
    const delay = (POINT_SPAWN_MS_MIN + Math.random() * (POINT_SPAWN_MS_MAX - POINT_SPAWN_MS_MIN)) / AMBIENT
    spawnTimer = setTimeout(() => { pointRipples.push(new PointRipple()); schedulePointRipple() }, delay)
  }

  function draw() {
    frameN++
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    if (Math.random() < WAVE_SPAWN * AMBIENT) waveRipples.push(new WaveRipple())
    for (let i = waveRipples.length - 1; i >= 0; i--) { waveRipples[i].radius += waveRipples[i].speed; if (waveRipples[i].dead) waveRipples.splice(i, 1) }
    for (let i = pointRipples.length - 1; i >= 0; i--) { pointRipples[i].radius += pointRipples[i].speed; if (pointRipples[i].dead) pointRipples.splice(i, 1) }
    const now = performance.now()
    if (cursorFollow) {
      const moving = curActive && curTX != null && (now - lastMove) < CURSOR_IDLE_MS
      if (moving) {
        if (curX == null) { curX = curTX; curY = curTY }
        curX += (curTX - curX) * CURSOR_EASE; curY += (curTY - curY) * CURSOR_EASE
        curGlow = Math.min(1, curGlow + CURSOR_GLOW_UP)
        trail.unshift({ x: curX, y: curY })
        if (trail.length > TRAIL_LEN) trail.pop()
      } else {
        curGlow = Math.max(0, curGlow - CURSOR_GLOW_DOWN)
        if (trail.length) { trail.pop(); trail.pop() }
        if (curGlow <= 0.01) { trail.length = 0; curX = null }
      }
      for (let i = clickBursts.length - 1; i >= 0; i--) {
        const b = clickBursts[i], age = now - b.t0
        if (age >= CLICK_LIFE_MS) { clickBursts.splice(i, 1); continue }
        const progress = age / CLICK_LIFE_MS
        b.radius = CLICK_MAXR * (1 - Math.pow(1 - progress, 2.6))
        b.alpha = Math.pow(1 - progress, 1.25) * CLICK_AMP
      }
    }
    ctx.font = `${CELL - 2}px "JetBrains Mono","Courier New",monospace`
    for (let row = 0; row < rows; row++) {
      const py = row * CELL + CELL
      for (let col = 0; col < cols; col++) {
        const px = col * CELL + CELL * 0.5
        const cell = cells[row * cols + col]
        let wTotal = 0, wPeak = 0
        for (const r of waveRipples) { const i = r.intensityAt(px, py); if (i === 0) continue; wTotal = Math.min(1, wTotal + i); if (i > wPeak) wPeak = i }
        if (wTotal > 0.03) { cell.waveOp = Math.max(cell.waveOp, wTotal); if (wPeak > WAVE_MUTATE && Math.random() < 0.35) cell.char = randChar() }
        else { cell.waveOp = Math.max(0, cell.waveOp - WAVE_DECAY) }
        let pTotal = 0
        for (const r of pointRipples) { const i = r.intensityAt(px, py); if (i > 0) pTotal = Math.min(1, pTotal + i) }
        if (pTotal > 0.02) { cell.pointOp = Math.max(cell.pointOp, pTotal); if (Math.random() < 0.55) cell.char = randChar() }
        else { cell.pointOp = Math.max(0, cell.pointOp - POINT_DECAY) }
        if (cursorFollow) {
          if (cell.curOp > 0) cell.curOp = Math.max(0, cell.curOp - CURSOR_CELL_DECAY)
          if (curGlow > 0.01 && trail.length) {
            let best = 0, bdx = 0, bdy = 0, bcd = 0
            for (let k = 0; k < trail.length; k++) {
              const tp = trail[k], sway = k / TRAIL_LEN
              const wob = Math.sin(now * 0.004 + k * 0.55) * TRAIL_WOBBLE * sway
              const dx = px - (tp.x + wob), dy = py - (tp.y - wob * 0.6)
              const rk = CURSOR_R * (1 - TRAIL_TAPER * sway)
              if (dx < -rk || dx > rk || dy < -rk || dy > rk) continue
              const cd = Math.sqrt(dx * dx + dy * dy)
              if (cd >= rk) continue
              const falloff = 0.5 * (1 + Math.cos(Math.PI * cd / rk))
              const g = Math.pow(falloff, CURSOR_FOCUS) * (1 - sway * 0.5)
              if (g > best) { best = g; bdx = dx; bdy = dy; bcd = cd }
            }
            if (best > 0) {
              const flow = 1 - FLOW_DEPTH * 0.5 * (1 - Math.cos(bcd * FLOW_FREQ - now * FLOW_SPEED))
              const lit = best * CURSOR_AMP * curGlow * flow
              if (lit > cell.curOp) cell.curOp = lit
              cell.char = (best > 0.06 && curGlow > CURSOR_DASH_AT) ? dirGlyph(bdx, bdy) : '-'
            }
          }
          if (cell.curOp > 0 && curGlow <= CURSOR_DASH_AT) cell.char = '-'
        }
        let clkOp = 0
        for (const b of clickBursts) {
          const bdx = px - b.x, bdy = py - b.y, bd = Math.sqrt(bdx * bdx + bdy * bdy)
          const age = now - b.t0
          let burstOp = 0
          for (let ring = 0; ring < CLICK_RING_COUNT; ring++) {
            const ringRadius = b.radius - ring * CLICK_RING_GAP
            if (ringRadius <= 0) continue
            const edge = Math.abs(bd - ringRadius)
            if (edge >= CLICK_RING_THICKNESS) continue
            const ringFade = 1 - ring / (CLICK_RING_COUNT + 1)
            const ringOp = 0.5 * (1 + Math.cos(Math.PI * edge / CLICK_RING_THICKNESS)) * b.alpha * ringFade
            if (ringOp > burstOp) burstOp = ringOp
          }
          if (age < CLICK_CENTER_LIFE_MS) {
            const centerProgress = age / CLICK_CENTER_LIFE_MS
            const centerRadius = CLICK_CENTER_MAXR * (0.35 + centerProgress * 0.65)
            if (bd < centerRadius) {
              const centerOp = 0.5 * (1 + Math.cos(Math.PI * bd / centerRadius)) * (1 - centerProgress) * 0.72
              if (centerOp > burstOp) burstOp = centerOp
            }
          }
          if (burstOp > clkOp) {
            clkOp = burstOp
            if (burstOp > 0.12) cell.char = ringGlyph(bdx, bdy)
          }
        }
        const glowOp = cell.pointOp + cell.curOp + clkOp
        const finalOp = Math.min(1, BASE_OP + cell.waveOp + glowOp)
        if (finalOp < 0.02) continue
        ctx.fillStyle = `rgba(255,255,255,${finalOp.toFixed(3)})`
        if (glowOp > 0.10) { ctx.shadowColor = 'rgba(255,255,255,0.95)'; ctx.shadowBlur = 13 }
        ctx.fillText(cell.char, col * CELL, py)
        if (glowOp > 0.10) ctx.shadowBlur = 0
      }
    }
  }

  const FRAME_MS = 1000 / (cursorFollow ? 60 : 30)
  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  let lastFrame = 0, inView = true, pageVisible = !document.hidden
  function loop(ts) {
    raf = requestAnimationFrame(loop)
    if (ts - lastFrame < FRAME_MS) return
    lastFrame = ts
    draw()
  }
  function start() { if (raf == null && !reduceMotion) { lastFrame = 0; raf = requestAnimationFrame(loop) } }
  function stop() { if (raf != null) { cancelAnimationFrame(raf); raf = null } }
  function sync() { (inView && pageVisible) ? start() : stop() }

  const io = new IntersectionObserver(([e]) => { inView = e.isIntersecting; sync() }, { threshold: 0 })
  const onVisibility = () => { pageVisible = !document.hidden; sync() }
  const onResize = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { stop(); init(); reduceMotion ? draw() : start() }, 150) }

  const onMouseMove = (e) => {
    const rect = canvas.getBoundingClientRect()
    const x = e.clientX - rect.left, y = e.clientY - rect.top
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) { curActive = false; return }
    curTX = x * (canvas.width / rect.width); curTY = y * (canvas.height / rect.height)
    curActive = true; lastMove = performance.now()
  }
  const onMouseOut = (e) => { if (!e.relatedTarget && !e.toElement) { curActive = false; curX = null } }
  const onPointerDown = (e) => {
    if (e.button !== 0) return
    const rect = canvas.getBoundingClientRect()
    const x = e.clientX - rect.left, y = e.clientY - rect.top
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) return
    clickBursts.push({ x: x * (canvas.width / rect.width), y: y * (canvas.height / rect.height), t0: performance.now(), radius: 0, alpha: CLICK_AMP })
    if (clickBursts.length > 6) clickBursts.shift()
  }

  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibility)
  // The canvas box can change height without a window resize (its container grows/shrinks
  // with page content), which would otherwise scale the fixed-resolution bitmap and blow
  // the glyphs up. Re-init at the true pixel size whenever the element itself resizes.
  const ro = (typeof ResizeObserver !== 'undefined') ? new ResizeObserver(onResize) : null
  if (ro) ro.observe(canvas)
  if (cursorFollow) {
    window.addEventListener('mousemove', onMouseMove, { passive: true })
    document.addEventListener('mouseout', onMouseOut)
    window.addEventListener('pointerdown', onPointerDown, { passive: true })
  }
  io.observe(canvas)
  init()
  if (!cursorFollow) pointRipples.push(new PointRipple())
  if (!reduceMotion) { schedulePointRipple(); start() } else { draw() }

  return () => { stop(); io.disconnect(); if (ro) ro.disconnect(); clearTimeout(spawnTimer); clearTimeout(resizeTimer); window.removeEventListener('resize', onResize); document.removeEventListener('visibilitychange', onVisibility); if (cursorFollow) { window.removeEventListener('mousemove', onMouseMove); document.removeEventListener('mouseout', onMouseOut); window.removeEventListener('pointerdown', onPointerDown) } }
}

export default function MatrixCanvas({ cursor = false, className = '' }) {
  const ref = useRef(null)
  useEffect(() => {
    if (!ref.current) return undefined
    return runMatrixCanvas(ref.current, { cursor })
  }, [cursor])
  return <canvas ref={ref} aria-hidden="true" className={className} />
}
