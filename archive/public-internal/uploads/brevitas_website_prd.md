# Brevitas Systems — Website PRD

**Document:** Website v1 Product Requirements Document
**Purpose:** Implementation brief to hand to Claude (or any competent design/build collaborator) to produce the full marketing site + waitlist for Brevitas Systems.
**Status:** Working draft — ready to build from.
**Author:** James (via PRD synthesis)
**Date:** April 23, 2026
**Reading time:** ~25 minutes. **Skimmers start at §3 (IA) and §9 (page-by-page specs).**

---

## Table of contents

1. Objectives, non-objectives, and success criteria
2. Audience, traffic assumptions, and conversion model
3. Information architecture — the site map
4. Brand foundations — the visual system
5. Motion principles — what "cinematic but restrained" means here
6. Reference library — what to steal and what NOT to steal
7. Signature animations (three of them, speccd in detail)
8. Component library — reusable primitives
9. Page-by-page specifications
   - 9.1 Landing (`/`)
   - 9.2 Product (`/product`)
   - 9.3 How it works (`/how-it-works`)
   - 9.4 Benchmarks (`/benchmarks`)
   - 9.5 Docs (`/docs`) — stub
   - 9.6 Blog (`/blog`) — stub
   - 9.7 Waitlist (`/waitlist`)
10. Waitlist — form, validation, backend, post-submit
11. Copy library — every headline, subhead, and CTA on the site
12. Technical stack — recommendations and constraints
13. Accessibility, performance, SEO
14. Analytics and experimentation
15. Build phases and priority
16. Open questions and founder decisions still needed

---

## 1. Objectives, non-objectives, and success criteria

### 1.1 What this site is for

Brevitas Systems is a pre-seed multi-agent LLM infrastructure company. The Python SDK works, the first-run benchmarks exist (59.4% token reduction, 46.9% cost reduction, ~99% quality parity), and the fundraising push is next. The website has **four jobs**, in priority order:

1. **Convince a technical buyer, in under 60 seconds, that the layer is real, that it works, and that it doesn't break their stack.** The three questions from the one-pager — *does it work, does it break my stack, is it worth the switching cost* — must each be answered above the fold on Landing, and again in depth on Product / How-it-works / Benchmarks.
2. **Capture a qualified waitlist** that the founder can convert into design-partner conversations. Waitlist quality matters more than waitlist volume.
3. **Act as a credibility asset during the seed raise.** Investors will open the site before the first meeting. It has to read infrastructure-grade — on par with Modal, Baseten, Stripe — not "pre-seed AI deck."
4. **Establish Brevitas's category narrative** publicly: multi-agent is the default architecture now, the inter-agent communication layer is the missing piece, Brevitas owns it.

### 1.2 What this site is NOT

- **Not a product demo.** No interactive playground in v1. The SDK is real but not yet self-serve. Don't pretend it is.
- **Not a sales site.** No pricing page, no "Contact sales," no ROI calculator in v1. (ROI calculator is a v2 candidate.)
- **Not a blog-first content site.** Blog is a stub in v1 with one or two founder posts.
- **Not a generic "AI SaaS" template.** The whole visual system must feel like infrastructure, not LLM-hype.

### 1.3 Success criteria

These are targets, not guarantees, calibrated for pre-seed stage:

| Metric | Target (first 60 days) |
|---|---|
| Lighthouse performance (mobile) | ≥ 90 |
| Lighthouse accessibility | ≥ 95 |
| Time to first contentful paint | < 1.2s on a cold 4G connection |
| Waitlist conversion (visitor → submitted email) | 3–6% on warm traffic (HN, Twitter, referrals) |
| Qualified waitlist signups (role = engineering lead / founder / platform lead at a company building multi-agent pipelines) | ≥ 100 in first 60 days |
| Investor reaction (qualitative) | At least 3 unsolicited "nice site" mentions in meetings. Not a KPI. Still the one that matters. |

---

## 2. Audience, traffic assumptions, and conversion model

### 2.1 Who shows up

Ranked by volume, then by value. These come directly from §4 of the Problem One-Pager:

**1. Coding-agent / coding-copilot founders.** Cursor-likes, Devin-likes, Codegen-likes. Every customer task is 10+ inter-agent calls. They live and die on token economics. **Highest-value cohort. Design every page for this reader first.**

**2. Vertical agent startup founders and CTOs.** Legal, medical, sales, research agents. Agent pipelines *are* their product. High intent, smaller teams, faster to pilot.

**3. Enterprise AI platform leads.** Platform engineering at Series B–D AI-native companies. They own the infra bill and will be asked about COGS at their next board.

**4. Investors.** Pre-seed, seed, and a few funds with a multi-agent thesis (Conviction, South Park Commons, Basis Set, Essence VC, Founders Fund, etc.). They read the site end-to-end including the footer.

**5. Curious engineers, researchers, HN lurkers.** Will not convert. Will share. Treat as distribution, not conversion.

### 2.2 Traffic assumptions

At launch, traffic will be small and bursty — driven by founder posts on Twitter/X, HN, LinkedIn, and direct shares in investor intros. Design for this reality:

- **First-visit readers.** Everyone who lands has never heard of Brevitas. Don't assume context.
- **Mobile-heavy on social days, desktop-heavy on weekdays.** Full responsive parity required.
- **Scroll-depth will be high among qualified readers.** The top 10% of visitors will read every page. Reward them with depth.

### 2.3 Conversion model

```
Social / HN / direct  →  Landing  →  one of three jumps:
                                      ↳ How it works  → Waitlist
                                      ↳ Benchmarks   → Waitlist
                                      ↳ Product       → Waitlist
```

Landing's job is to convert the skimmer into a reader. Product / How it works / Benchmarks convert the reader into a signup. The CTA is always the same — **Join the waitlist** — but the context that precedes it shifts per page.

---

## 3. Information architecture — the site map

### 3.1 Pages

```
/                       Landing
/product                Product — what the SDK does, integration shape, one code example
/how-it-works           How it works — the six techniques + architecture diagram + multi-agent animation
/benchmarks             Benchmarks — AgentBench, MARBLE, BattleAgentBench numbers and methodology
/docs                   Docs — stub page pointing to a waitlist for early-access docs access
/blog                   Blog — index + 1–2 founder posts at launch
/blog/[slug]            Blog post
/waitlist               Waitlist — form + confirmation states
/legal/privacy          Privacy policy
/legal/terms            Terms (minimal, because there's no self-serve product yet)
404                     Not found — treat as a secondary brand moment, not a dead end
```

### 3.2 Global navigation

**Top nav (desktop):**

```
[logo]  Product   How it works   Benchmarks   Docs   Blog          [Join waitlist →]
```

- Logo on the far left. No wordmark lockup on mobile — mark only.
- Five nav items plus the CTA. The CTA is the only visually weighted element in the nav.
- No dropdowns. No mega-menu. Nav hides on scroll-down and returns on scroll-up (standard pattern).
- On mobile: hamburger → full-screen sheet menu. The sheet is its own brand moment — serif display-size links, not a stack of sans-serif items.

**Footer (every page):**

Four columns on desktop, single-column collapsible on mobile.

```
Brevitas Systems                  Product              Company             Stay in the loop
[mark]                            Product              About               [email input]
Brevitas — brevitas (Latin)        How it works         Blog                [Join →]
"shortness, concision."           Benchmarks           Contact
                                  Docs
                                  Changelog

                    ©2026 Brevitas Systems. All rights reserved.        Privacy   Terms   [status: operational]
```

- Footer has its own small-format waitlist input. This is the secondary conversion surface.
- "Status: operational" is a small dot + text, styled like a minimal status page badge. Even with no public status page yet, this signals infra-seriousness. (When the product ships publicly, link it to a real status endpoint.)

---

## 4. Brand foundations — the visual system

Extending and slightly modernizing the existing deck brand (see `theme.css` in the remix bundle). The deck is editorial-classical; the web is **editorial-technical**. Same palette, same typography, a touch more motion and a touch more monospace.

### 4.1 Color tokens

Reuse the existing deck tokens verbatim. These are the source of truth:

```css
:root {
  --ink: #0f1410;          /* Primary background */
  --ink-2: #161b17;        /* Secondary surface (cards, code blocks) */
  --ink-3: #1e231f;        /* Tertiary surface (hover states, deep panels) */
  --line: #2a2f2a;         /* Dividers, hairlines */
  --stone: #716b5e;        /* Quiet text */
  --stone-2: #9a948a;      /* Body-dim text */
  --bone: #e8e2d5;         /* Primary foreground */
  --bone-dim: #c7c2b6;     /* Secondary foreground */
  --bronze: #d4a84b;       /* Accent — reserved, not decorative */
  --bronze-deep: #a5802f;  /* Accent hover / pressed */
  --oxblood: #8b2e2e;      /* Warning / negative delta */
}
```

**Add one web-specific accent** — a reserved signal green used for **positive deltas, compression highlights, and nothing else**:

```css
  --signal: #a8c98b;       /* Matte signal green — used on benchmark positive deltas and compression-accepted tokens */
  --signal-glow: rgba(168, 201, 139, 0.12);  /* For soft halos on hero animations only */
```

**Usage discipline:**
- Bronze is the brand accent. Use on CTAs, the accent dot in tags, and hover states only. Never on body text, never on decorative rules.
- Signal green is reserved for *data-true* positive markers (e.g., "tokens kept" in the compression animation, "-59.4%" on the hero stats). Never on CTAs, never on nav.
- Oxblood appears once, maybe twice — on the baseline ("before") side of comparison diagrams to show waste. Not on error messages (error messages get a neutral charcoal-plus-icon treatment).

### 4.2 Typography

Three families, each with one job:

| Role | Family | Weight | Notes |
|---|---|---|---|
| Display / editorial | Newsreader (variable) | 300 (thin) | `font-variation-settings: "opsz" 36;` — keep the optical-size trick from the deck |
| UI / body | Inter Tight | 400, 500, 600 | `font-feature-settings: "ss01", "cv11";` |
| Monospace / data | JetBrains Mono | 400, 500 | Use for code, numbers in comparison tables, inline tech terms |

**Display type rules for web (different from deck):**

- **Hero displays on landing are serif but smaller than the deck.** `clamp(56px, 9vw, 120px)` — never the 168px deck size. The web is closer to the reader than a projected slide.
- **Section headers on interior pages use Newsreader at h2 size (56px desktop / 40px mobile).**
- **Monospace is a first-class display face on this site.** Section overlines, benchmark table numbers, the compression animation, and the terminal examples are all set in JetBrains Mono. This is the "technical" lean the web version gets that the deck doesn't.

**Type scale (desktop → mobile):**

```
display-xl   : 120px → 64px    Newsreader 300
display      : 88px  → 52px    Newsreader 300
h1           : 64px  → 40px    Newsreader 400
h2           : 44px  → 32px    Newsreader 400
h3           : 32px  → 24px    Inter Tight 500 (sans-serif!) on web — lighter than deck
body-lg      : 22px  → 18px    Inter Tight 400, line-height 1.55
body         : 17px  → 16px    Inter Tight 400, line-height 1.62
small        : 14px  → 13px    Inter Tight 400, color: stone-2
mono-lg      : 18px  → 16px    JetBrains Mono 400
mono         : 14px  → 13px    JetBrains Mono 400
overline     : 12px  → 12px    JetBrains Mono 500, uppercase, letter-spacing 0.18em
```

### 4.3 Surfaces, borders, and spacing

- **Page background:** `--ink` everywhere. No gradient sections unless specifically called out (e.g., waitlist success state).
- **Card surfaces:** `--ink-2` with a 1px border in `--line`. Corner radius `4px` — tight, not rounded-friendly. This is infrastructure; it doesn't need pillow corners.
- **Dividers:** 1px `--line`. On narrow columns, use 32px vertical whitespace instead of a rule.
- **Grid:** 12-column, 72px gutters desktop, 16px mobile. Max content width 1280px. Narrow-text columns (prose) cap at 640px for readability.
- **Vertical rhythm:** Section padding `clamp(96px, 12vh, 200px)` top and bottom. Generous. This is an editorial layout, not a SaaS screen.

### 4.4 The mark

The existing logo mark — a squared Roman-capital-style "B" implied by a square outline with a horizontal and vertical rule crossing it — is in `theme.css` as `.logo-mark`. Reuse it verbatim. Never scale below 28px. Never apply color other than `--fg` or `--bg`.

Wordmark (`.logo-word`) is Newsreader 500, 26px. Use on landing hero and footer. **Never in the nav** — the mark is enough there.

### 4.5 Iconography

**No icon library in v1.** No Lucide, no Heroicons, no Material.

Icons that appear on the site are either:
- Drawn inline as SVG, matched to `--fg` at 1.5px stroke, no fill unless the icon *is* the fill (e.g., the benchmark-logo shields).
- Or small typographic symbols — `→`, `·`, `+`, `−` — in Inter Tight or JetBrains Mono.

If an icon set is truly needed (e.g., for a features grid), use **Phosphor Icons "thin" weight** — the 1px-stroke variant. It matches the editorial weight of the type. **Only the thin weight.**

---

## 5. Motion principles — what "cinematic but restrained" means here

Reference axis: Vercel and Cartesia, not Blade Runner and not a Framer template library. Motion is in service of comprehension, not decoration.

### 5.1 The five rules

1. **Every animation must answer a question the user is already asking.** The compression animation answers *"what does your thing do to my text?"* The pipeline animation answers *"where does your layer sit?"* The number-counter animation answers *"is that 59% real or rounded?"* If an animation isn't answering a question, cut it.

2. **No parallax. No scroll-jacking. No horizontal scroll.** The reader controls the page. Scroll-triggered reveals are fine — locking the page while a helicopter flies past is not.

3. **Motion budget per viewport: one hero moment, up to three micro-interactions.** A single full viewport should not have multiple large animations competing. If the pipeline animation is running, the surrounding text fades in but doesn't compete.

4. **Everything respects `prefers-reduced-motion: reduce`.** Full stop. When the preference is set, animations collapse to instant state changes or ≤100ms fades. No opt-in toggle needed — we respect the OS setting.

5. **Timings and easing are a single system, not per-component guesses.** Use these, and only these:

```css
/* Durations */
--dur-xs: 120ms;    /* Micro — hover states, focus rings */
--dur-sm: 240ms;    /* Small — button presses, input focus */
--dur-md: 420ms;    /* Medium — section reveals, number counts */
--dur-lg: 720ms;    /* Large — hero animation frames, pipeline transitions */
--dur-xl: 1200ms;   /* Cinematic — full compression sequence, title entrances */

/* Easings */
--ease-out-standard: cubic-bezier(0.16, 1, 0.3, 1);   /* Default — decelerates naturally */
--ease-out-soft:     cubic-bezier(0.22, 1, 0.36, 1);  /* For fades */
--ease-in-out-data:  cubic-bezier(0.65, 0, 0.35, 1);  /* For number counters and chart draws */
--ease-step:         steps(24);                       /* For typewriter effects only */
```

### 5.2 What "cinematic" buys you (and what it doesn't)

It buys:
- **One hero animation per page, unapologetically large.** On Landing: the multi-agent reduction animation. On How-it-works: the technique-by-technique stack. On Benchmarks: the delta-bar draw-in.
- **Layered entrances.** Text, numbers, and rules stagger in with 60–120ms gaps. Not a wall of fade-in.
- **Scroll-linked opacity on the hero text.** Not scroll-linked scale or translate. Opacity only, over a short range.
- **Numerical counters that tick up** when a stats card enters view, once, with deterministic duration.

It does not buy:
- Particle fields.
- Animated noise.
- Glowing orbs.
- Rotating 3D models.
- Background video loops.
- Mouse-following anything except the text cursor on interactive terminal examples.

---

## 6. Reference library — what to steal and what NOT to steal

### 6.1 Steal these patterns

| Reference | URL | What to take | What NOT to take |
|---|---|---|---|
| **The Token Company** | thetokencompany.com | The **compression animation** — monospace text with "dropped" words shown as ghost-gaps, accepted words highlighted. Core visual for our compression demo. | Their product positioning (they're single-call; we're multi-agent). Our animation must visibly operate on a *pipeline*, not a prompt. |
| **Linear** | linear.app | The restraint. Subtle scroll reveals. How they use monospace for in-product data. Nav behavior. | Their blue accent. Their product-screenshot-heavy layout. |
| **Vercel** | vercel.com | Layered text entrances. The way they treat code blocks as first-class hero content. Their use of thin lines between sections. | Their gradient meshes. Their "ship, ship, ship" tone. |
| **Cartesia** | cartesia.ai | Cinematic hero moments without being gaudy. Bold typography mixed with motion that has meaning. | Their purple. Their product is consumer-adjacent; ours is pure infra. |
| **Stripe** | stripe.com | Credibility engineering — small moves, tight code examples, honest numbers, premium density. How they make docs beautiful. | Their gradient color work. Their breadth of SKUs. |
| **Modal** | modal.com | How they sell "serverless compute for Python" with a single code block above the fold and prose below. Tone is playful-technical — ours should be serious-technical, but the structure works. | Their pink/magenta accent. Their density of logos — we don't have that yet. |
| **Baseten** | baseten.co | How they sequence *problem → product → proof*. Their use of a simple architecture diagram. | Their illustration style (cutesy in spots). |
| **Resend** | resend.com | The weight of their typography. The way they use one hero color as the page's only non-mono non-neutral. | Their monochrome product shots — we don't have a dashboard to show. |
| **Exa (formerly Metaphor)** | exa.ai | Benchmark page structure. How they present research credibility honestly. | Their animated hero — too much for us. |
| **Anthropic** | anthropic.com | Editorial restraint. How they make a research company feel stable. | Their beige/cream on light — we're dark. |

### 6.2 Do not imitate any of these

- **Generic AI-SaaS Framer templates.** If something looks like it came from a Framer AI-startup template ("Get started with the future of AI"), it's wrong. Our site should look individually designed.
- **Gradient mesh backgrounds.** Overused. Signals "I used a template."
- **Matrix rain / code rain.** See §10 of the one-pager — no Blade Runner.
- **Neural-net hero illustrations** (nodes and edges with glowing particles). Cliché.
- **"AI brain" imagery of any kind.** Nope.
- **Fake product screenshots with Lorem Ipsum.** The SDK exists. Real code, real commands, or no UI at all.
- **Marketing-speak headlines.** "Unlock the power of your multi-agent pipelines" is banned. If a sentence could appear on a Salesforce page, delete it.

### 6.3 Ten-second test

Before shipping a section, ask: *if this exact screenshot appeared in a feed next to screenshots of Modal, Baseten, and Linear, would it look like it belongs there, or like the odd one out?* If it's the odd one out, fix it.

---

## 7. Signature animations (three of them, specced in detail)

These are the three motion pieces that carry the site. Everything else is supporting.

### 7.1 Animation A — **The Compression Sequence** (Landing, above-the-fold supporting visual)

**What it is:** A demonstration of inter-agent message compression. A block of natural-language "agent output" appears fully rendered. Then words that don't carry meaning drop out — leaving visible ghost-gaps where they were — while words that carry meaning remain and briefly highlight in signal-green. A running counter in the corner ticks from "1,000 tokens" down to "300 tokens."

**Why it matters:** This is the single most legible "what the layer does" visual. It takes the most abstract part of the pitch ("we compress inter-agent communication without losing meaning") and makes it watchable.

**Visual spec:**

```
┌───────────────────────────────────────────────────────────────┐
│  AGENT 1 OUTPUT                    ╱ BEFORE                   │
│                                    ╲ 1,000 TOKENS             │
│                                                               │
│  The architecture should follow a three-layer pattern         │
│  comprising a presentation layer, a business logic layer,     │
│  and a persistence layer. The presentation layer handles      │
│  user-facing concerns and renders the UI based on props       │
│  passed down from the business layer. The business logic      │
│  layer encapsulates domain operations and should be           │
│  written in pure functions where possible. The persistence    │
│  layer mediates reads and writes against the database...      │
└───────────────────────────────────────────────────────────────┘

                              ↓ (t = 800ms hold, then begin dropout)

┌───────────────────────────────────────────────────────────────┐
│  COMPRESSED                        ╱ AFTER                    │
│                                    ╲ 300 TOKENS   –70%        │
│                                                               │
│     architecture     three-layer pattern                      │
│       presentation           business logic                   │
│       persistence.                                            │
│       presentation     user-facing                            │
│                     UI                                        │
│       business logic                                          │
│        domain operations           pure functions             │
│       persistence       reads                   writes        │
│        database...                                            │
└───────────────────────────────────────────────────────────────┘
```

**Motion timing:**

| Phase | Duration | What happens |
|---|---|---|
| t = 0 – 400ms | 400ms | Container fades in. "BEFORE 1,000 TOKENS" label types in as mono. |
| t = 400 – 1200ms | 800ms | Full paragraph types in at `--ease-step` (steps-24). Feels like a terminal, not a human typing. |
| t = 1200 – 2000ms | 800ms | Hold. Let the reader absorb the paragraph. |
| t = 2000 – 3400ms | 1400ms | **Dropout phase.** Non-meaning words fade to transparent over 280ms each, staggered by 60ms. Their space collapses gradually — the remaining words subtly drift toward each other but never fully close the gap (we want the *absence* to be visible). Meaning-bearing words briefly get a `--signal` color flash at 200ms peak, then settle to `--fg`. |
| t = 3400 – 3800ms | 400ms | Counter ticks from 1000 → 300 over 400ms with `--ease-in-out-data`. Delta label "–70%" fades in. |
| t = 3800ms onward | — | Hold indefinitely. Button below: `[ Replay ↻ ]` in mono, subtle. |

**The word dropout rule (for the drafting designer):** the "kept" words should feel like they were *selected*, not arbitrarily left. Kept words carry meaning: nouns, verbs, key technical terms. Dropped words are articles, filler, connective tissue. The draft below is what the designer should implement — the dropped words are shown as `~~strikethrough~~` here for reference only:

> ~~The~~ architecture ~~should follow a~~ three-layer pattern ~~comprising a~~ presentation ~~layer, a~~ business logic ~~layer, and a~~ persistence ~~layer. The~~ presentation ~~layer handles~~ user-facing ~~concerns and renders the~~ UI ~~based on props passed down from the~~ business logic ~~layer. The~~ business logic ~~layer encapsulates~~ domain operations ~~and should be written in~~ pure functions ~~where possible. The~~ persistence ~~layer mediates~~ reads ~~and~~ writes ~~against the~~ database…

The designer should produce this paragraph and its compressed version as a single piece of content with each word tagged `data-kept="true|false"`. The animation iterates over them and tunes opacity accordingly.

**Reduced-motion variant:** Both states shown side-by-side, no animation. Counter displays "1000 → 300 (–70%)" as static text.

**Placement:** Landing, supporting visual below the hero headline. Also reused on `/product` as an inline explainer within the "compression" technique card.

### 7.2 Animation B — **The Pipeline Reduction** (THE hero animation)

**What it is:** A side-by-side multi-agent pipeline animation. Left side is baseline (how teams build this today). Right side is with Brevitas. Both run the same three-agent task. Token counters tick up in real time. Baseline's counter balloons. Brevitas's counter stays calm. End state: a stats panel drops in with **–59.4%**, **–46.9%**, **99%**.

**Why it matters:** This is the "missing layer" visual the user explicitly asked for. It communicates the core thesis — multi-agent is the default now, Brevitas is the layer between the agents — in one continuous shot.

**Visual spec (desktop):**

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                      │
│   BASELINE PIPELINE                        │       WITH BREVITAS                     │
│   How every team builds this today         │       The same task, optimized          │
│                                            │                                         │
│                                            │                                         │
│   ┌─────────┐                              │       ┌─────────┐                       │
│   │ Agent 1 │                              │       │ Agent 1 │                       │
│   │Architect│                              │       │Architect│                       │
│   └────┬────┘                              │       └────┬────┘                       │
│        │ ═══════════                       │            │ ━━━                        │
│        │ 1,000 tok                         │            │ 300 tok  [compressed]      │
│        ▼                                   │            ▼                            │
│   ┌─────────┐                              │       ┌═════════┐                       │
│   │ Agent 2 │                              │       ║ BREVITAS║ ← shared memory       │
│   │ Builder │                              │       ║  LAYER  ║                       │
│   └────┬────┘                              │       └═════════┘                       │
│        │ ═══════════════════               │            │ ━━━                        │
│        │ 1,500 tok                         │            │ ▼                          │
│        ▼                                   │       ┌─────────┐                       │
│   ┌─────────┐                              │       │ Agent 2 │                       │
│   │ Agent 3 │                              │       │ Builder │                       │
│   │Reviewer │                              │       └────┬────┘                       │
│   └────┬────┘                              │            │ ━━━                        │
│        │                                   │            ▼                            │
│        ▼                                   │       ┌═════════┐                       │
│                                            │       ║ BREVITAS║                       │
│                                            │       ║  LAYER  ║                       │
│                                            │       └═════════┘                       │
│                                            │            │                            │
│                                            │            ▼                            │
│                                            │       ┌─────────┐                       │
│                                            │       │ Agent 3 │                       │
│                                            │       │Reviewer │                       │
│                                            │       └────┬────┘                       │
│                                            │                                         │
│   CUMULATIVE                               │       CUMULATIVE                        │
│   ▶ 2,924 tokens                           │       ▶ 1,188 tokens                    │
│                                            │                                         │
└────────────────────────────────────────────┴─────────────────────────────────────────┘

                                   ↓ (animation resolves)

┌──────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                      │
│               –59.4%           –46.9%           99%                                  │
│               tokens            cost            quality parity                       │
│               saved             saved           maintained                           │
│                                                                                      │
│               AgentBench task · real API calls · real token counts                   │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**Key visual language:**
- **Baseline arrows** = thick double-rules (`═══════════`). Width encodes volume. They grow thicker as context accumulates. Color: `--oxblood` at 40% opacity — this is the "waste" side, visually honest about cost.
- **Brevitas arrows** = single thin rules (`━━━`). Uniform thin width. Color: `--signal`. Calm, consistent.
- **The Brevitas layer** is a box between each pair of agents, styled as a double-stroke frame with "BREVITAS LAYER" in monospace overline. It is the *hero element*. On hover (post-animation), it can gently pulse to show it's the active piece.
- **Agent boxes** are identical on both sides. Same font, same size, same position. **The agents don't change. Only what's between them changes.** The visual grammar must reinforce this.

**Motion timing (total ~6s, auto-loops after 12s dwell):**

| Phase | Duration | What happens |
|---|---|---|
| t = 0 – 400ms | 400ms | Both columns fade in. Agent boxes appear first. The Brevitas layer boxes on the right fade in at 60% opacity. |
| t = 400 – 900ms | 500ms | Agent 1 (both sides) "activates" — a 2px `--fg` border briefly pulses. Arrow begins drawing from Agent 1 downward. |
| t = 900 – 1800ms | 900ms | Arrows draw downward on both sides. **Token counters tick up live** as the arrow draws. Left counter ticks to 1,000. Right counter ticks to 300. Left arrow thickens visibly as it draws. Right arrow stays thin. On the right, the Brevitas layer box it passes through briefly brightens, as if "catching" the context. |
| t = 1800 – 2700ms | 900ms | Agent 2 activates. Arrows draw from Agent 2 downward. Left counter adds +500 (now 1,500). Right counter adds ~500 but then *subtracts* to a delta (still visually shown as "+delta only"). Left arrow now visibly thicker than before. Right stays thin. |
| t = 2700 – 3600ms | 900ms | Agent 3 activates. Arrows complete. Cumulative total tickers finalize: **2,924** (left), **1,188** (right). |
| t = 3600 – 4400ms | 800ms | Hold. Lock the reader on the disparity. |
| t = 4400 – 5800ms | 1400ms | **Resolution frame.** The entire split view slides up and dims to 20% opacity. A stats panel fades in centered: "–59.4% / –46.9% / 99%" with the labels. Numbers count up from zero with `--ease-in-out-data` over 800ms. Caption below: "AgentBench task · real API calls · real token counts." |
| t = 5800ms onward | Dwell | Hold the stats panel for 12s, then auto-replay if still in viewport. |

**Controls:**
- A small mono-styled `[Replay ↻] [Pause ❚❚]` control sits below the animation, right-aligned. Respects focus-visible states.
- The animation auto-plays once when it enters the viewport. Auto-loop only if still in view.
- Pause on tab blur.

**Reduced-motion variant:** Both columns shown as static diagrams side-by-side with final token totals already present. The stats panel appears immediately below, no count-up. A single line of copy replaces the motion: "Same pipeline, same task. Left: how it's built today. Right: with Brevitas."

**Placement:** 
- Hero on `/` (Landing), full-bleed within a content container.
- Reused (without the resolution frame) at the top of `/how-it-works`.
- Reused (simplified, right column only, showing where in the pipeline each of the six techniques activates) inside `/how-it-works`.

### 7.3 Animation C — **The Thesis Sequence** (Landing, section 2)

**What it is:** A single-slide animated thesis statement about multi-agent architecture. Three stacked lines of large serif type fade in sequentially. Under each line, a supporting mono caption fades in. The final line is visually heavier — it states Brevitas's place in the stack.

**Why it matters:** The user explicitly asked for "animations for multi-agent systems like how the future is multi-agent companies but the missing layer blah blah blah." This is that piece. It is Brevitas's *category claim*.

**Visual spec (desktop):**

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  THE STATE OF THE STACK — 2026                                       │
│  ─────────────────────────────                                       │
│                                                                      │
│                                                                      │
│   The 2023 default was a single prompt.                              │
│   // gpt-4.turbo · one model · one call                              │
│                                                                      │
│                                                                      │
│   The 2026 default is a pipeline of agents.                          │
│   // orchestrators · 5 to 50 inter-agent calls per task              │
│                                                                      │
│                                                                      │
│   ────────────── and no one optimized                                │
│                                                                      │
│   what flows between them.                                           │
│   // until now                                                       │
│                                                                      │
│                                                                      │
│                                     [ See how → ]                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

**Motion timing (total ~5s, scroll-triggered, no auto-replay):**

| Phase | Duration | What happens |
|---|---|---|
| t = 0 – 500ms | 500ms | Section enters viewport. Overline "THE STATE OF THE STACK — 2026" and its thin rule fade in. |
| t = 500 – 1400ms | 900ms | Line 1 ("The 2023 default…") fades in and slides up 16px. Mono caption fades in 200ms after the line. |
| t = 1400 – 2300ms | 900ms | Line 2 ("The 2026 default…") enters the same way. Lines 1 still fully visible — this is accumulation, not replacement. |
| t = 2300 – 2500ms | 200ms | Brief pause. |
| t = 2500 – 3600ms | 1100ms | Line 3 ("and no one optimized what flows between them.") enters in two parts. First the connective "────── and no one optimized" types in as mono with `--ease-step`. Then "what flows between them." fades up in serif at a visibly larger size than lines 1 and 2. |
| t = 3600 – 4000ms | 400ms | "// until now" caption fades in below line 3. |
| t = 4000 – 5000ms | 1000ms | "See how →" button fades in, subtly brighter than the body. |

**Reduced-motion variant:** All three lines appear together, in their final state, on section enter. Rule of thumb: never degrade comprehension; just remove the sequence.

**Placement:** Landing only, section immediately following the hero.

### 7.4 Supporting micro-animations catalog

These appear across the site. Specify them once, reuse them everywhere.

- **Hover on primary CTA:** background shifts from `--bronze` to `--bronze-deep` over `--dur-sm` with `--ease-out-standard`. The arrow `→` inside the button translates 4px right with `--ease-out-standard` over `--dur-sm`.
- **Hover on nav link:** underline draws in left-to-right over `--dur-sm`. On blur it retracts right-to-left. (Use `transform-origin` trick, not width animation — cheaper.)
- **Input focus:** 1px border shifts from `--line` to `--fg` over `--dur-xs`. The label (if floating) shrinks and translates to top-left over `--dur-sm`.
- **Card hover on benchmark tiles:** the 1px border brightens from `--line` to `--stone-2` over `--dur-sm`. No lift, no shadow.
- **Number counter tick-up** (used on benchmarks and stats panels): `--dur-md` with `--ease-in-out-data`, rounded to destination value. Never animate decimal points character-by-character — animate the numeric value and re-render.
- **Code block syntax highlight** on the inline SDK example: no animation at rest. When the code is first in viewport, the import line types in over `--dur-md`, then the rest fades in whole. One-shot; don't loop.
- **Footer waitlist submit:** on success, the entire input row collapses into a single check + "You're on the list." over `--dur-md`, ink background, signal-green check. Reversible if needed.

---

## 8. Component library — reusable primitives

Build these once. Use them everywhere.

### 8.1 `<Overline />`

Mono, uppercase, 12px, letter-spacing 0.18em, `--stone-2`. Optional dot prefix (the `--bronze` tag dot) and optional trailing em-dash with light text. Used at the top of every section.

Examples:
- `• INFRASTRUCTURE LAYER` (bronze dot + mono)
- `THE STATE OF THE STACK — 2026` (plain mono)

### 8.2 `<StatCard />`

Container for a single statistic. Large serif number, mono label below, optional delta chip.

```
┌──────────────────────────┐
│                          │
│    –59.4%                │
│    ────                  │
│    tokens saved          │
│    per multi-agent task  │
│                          │
│    AgentBench · n=1 →    │ ← footnote link on benchmarks page
│                          │
└──────────────────────────┘
```

Variants: `default`, `emphasis` (larger, only used for the hero stats panel), `inline` (horizontal layout for footer-style stat strips).

Numbers render in tabular numerals with `font-variant-numeric: tabular-nums`. Otherwise the tick-up animation wobbles.

### 8.3 `<TechniqueCard />`

Used on `/how-it-works` and Landing's technique grid.

```
┌──────────────────────────────┐
│ 01                           │
│                              │
│ Inter-agent message          │
│ compression                  │
│                              │
│ Each agent's output is       │
│ compressed before being      │
│ passed downstream. Redundant │
│ sentences removed, task-     │
│ relevant structure preserved.│
│ Compression ratio tunable    │
│ per pipeline.                │
│                              │
│ [see it →]                   │
│                              │
└──────────────────────────────┘
```

- 01–06 index in JetBrains Mono, 12px, `--stone-2`.
- Title in Newsreader 400, 24px.
- Body in Inter Tight 400, 16px, `--bone-dim`.
- "see it →" only on cards that have an inline demo (compression does, others may not yet).

### 8.4 `<BenchmarkBadge />`

A small shield-style component that renders a benchmark name + venue. Used in the "validated on" trust row.

```
┌─────────────────────┐
│  ┌───┐              │
│  │ A │  AgentBench  │
│  └───┘  ICLR 2024   │
└─────────────────────┘
```

- Monogram letter in a square frame (border-only), 40px square.
- Name in Inter Tight 500, 15px.
- Venue in JetBrains Mono 12px, `--stone-2`.
- Three of these: **AgentBench (ICLR 2024)**, **MARBLE (ACL 2025)**, **BattleAgentBench**.

### 8.5 `<CodeBlock />`

For inline code on `/product` and `/docs`. Dark surface (`--ink-2`), 1px border `--line`, 20px padding, rounded 4px. Font JetBrains Mono 14px, line-height 1.6.

Syntax highlighting: minimal palette.
- Keywords (`import`, `from`, `def`, `return`): `--bone`.
- Strings: `--signal`.
- Numbers and comments: `--stone-2`.
- Everything else: `--bone-dim`.

**No colorful 8-token syntax theme.** This is a four-color theme on purpose — matches the brand.

The one-liner above the code block should feel like a terminal cue:

```
# pip install brevitas
```

followed by:

```python
from brevitas import optimize

pipeline = optimize(pipeline)
```

If `<CodeBlock />` is the hero on `/product`, it gets a copy button (`[⧉ copy]`) in the top-right, mono, very small.

### 8.6 `<Button />`

Three variants only.

**Primary** — `--bronze` background, `--ink` text, tight letterspacing, Inter Tight 500, 15px, 12px vertical × 20px horizontal padding, 2px corner radius, trailing `→`.

**Secondary** — transparent background, 1px `--fg` border, `--fg` text, same type spec as primary.

**Ghost** — transparent background, no border, `--fg-dim` text, underline on hover. Used for secondary nav and "see all →" links.

All three have identical hover motion: slight brightness shift on bg/border, trailing glyph translates 4px.

### 8.7 `<SectionShell />`

Every page section lives inside a `<SectionShell />`. It provides:
- Consistent vertical padding (`clamp(96px, 12vh, 200px)` top/bottom).
- Optional top thin-rule (`<hr class="rule" />`).
- Optional overline slot (renders an `<Overline />`).
- Grid layout (12-col, 72px gutters).

Pages are built by composing `<SectionShell />`s, not by writing bespoke layouts per section.

### 8.8 `<NavBar />` / `<Footer />`

Global. Already specced in §3.2. Build once.

### 8.9 `<WaitlistInput />`

Used in hero, at the bottom of every marketing page, and in the footer. Spec in §10.

---

## 9. Page-by-page specifications

Each page has: **purpose**, **sections in order**, **hero copy**, **body copy direction**, **motion notes**, and a **conversion anchor**.

### 9.1 Landing (`/`)

**Purpose:** Convert a skimmer into a reader in 60 seconds. Answer *does it work / does it break my stack / is it worth the switching cost* above the fold.

**Conversion anchor:** Primary CTA "Join the waitlist →" in hero. Secondary CTA "See the benchmarks →" linking to `/benchmarks`. Footer waitlist input as safety net.

**Sections, top to bottom:**

**S1 — Hero.**
- Overline: `• INFRASTRUCTURE LAYER FOR MULTI-AGENT PIPELINES`
- Headline (Newsreader 300, display-xl): **The communication layer between your agents.**
- Subhead (Inter Tight 400, body-lg, max-width 640px): *Multi-agent pipelines lose 60% of their tokens to redundant inter-agent context. Brevitas gives it back — as a drop-in Python SDK, without touching your agents, prompts, or models.*
- Primary CTA: `[ Join the waitlist → ]`
- Secondary CTA: `See the benchmarks →` (ghost)
- Hero stats strip below the CTAs, three `<StatCard />` inline variants:
  - `–59.4%` tokens saved
  - `–46.9%` cost saved
  - `99%` quality parity
  - Small footnote: `First-run benchmark · AgentBench task · real API calls`

**S2 — The Thesis Sequence.** Full viewport. See §7.3.

**S3 — The Pipeline Reduction Animation.** Full viewport. See §7.2.

**S4 — Compression explainer.**
- Overline: `HOW THE LAYER WORKS, IN ONE VIEW`
- H2: **Every message between agents is compressed, referenced, or sent as a delta. Your agents don't change. The tokens between them drop by half.**
- Left column: prose explanation (3 short paragraphs). Right column: the **Compression Sequence** animation (§7.1).
- Mobile: animation on top, prose below.

**S5 — Six techniques overview.**
- Overline: `SIX STACKABLE TECHNIQUES`
- H2: **One layer, six techniques. Task-aware routing decides which to apply per call.**
- Grid of six `<TechniqueCard />`:
  1. Inter-agent message compression
  2. Shared memory with content-addressed references
  3. Delta mode
  4. Smart context pruning
  5. Compact message protocol
  6. Task-aware routing
- Each card has a 2–3 line body copy. "see it →" link only on card 1 (compression, which links to `/how-it-works#compression`).
- Full details live on `/how-it-works`. CTA below the grid: `Deep dive into each technique →`

**S6 — Integration proof.**
- Overline: `INTEGRATION SURFACE`
- Split layout: Left column: copy. Right column: `<CodeBlock />`.
- Copy (left):
  - H2: **Import. Wrap. Ship.**
  - Body: *Brevitas wraps your existing multi-agent orchestration. Keep your agents. Keep your prompts. Keep your models. Drop-in compatible with LangGraph, CrewAI, AutoGen, and custom pipelines. Provider-agnostic — validated against DeepSeek, Groq, and OpenAI running simultaneously in a three-agent pipeline.*
- Code block (right), terminal + python:

```
# pip install brevitas
```

```python
from brevitas import optimize
from my_pipeline import architect, builder, reviewer

pipeline = optimize([architect, builder, reviewer])
result = pipeline.run(task)
```

*(This is illustrative. The actual public API should match what the SDK ships with. If the API is different, use the real shape — never fake code on a marketing site.)*

**S7 — Benchmarks trust row.**
- Overline: `VALIDATED ON`
- Three `<BenchmarkBadge />` components in a row: AgentBench, MARBLE, BattleAgentBench.
- Caption: *Three peer-reviewed multi-agent benchmarks integrated into our test harness.*
- Ghost CTA: `See full results →` → `/benchmarks`.

**S8 — Why now.**
- Overline: `WHY NOW`
- H2: **Three inflection points make the first half of 2026 the right moment.**
- Three-column list (not a card grid — a numbered editorial list, tighter):
  1. **Multi-agent crossed from demo to default.** Production pipelines now run 5–50 inter-agent calls per user task. The redundancy scales with the number of agents.
  2. **Cost accountability arrived.** AI spend is a real line item in 2026 P&Ls. Engineers have OKRs tied to inference cost. CFOs ask for breakdowns.
  3. **The tooling gap is visible — and closing.** Observability has winners. Routing has winners. Inter-agent optimization has no one. The category window is open but won't stay open.

**S9 — Final CTA.**
- Full-bleed, `--ink-2` background panel.
- H1 (Newsreader): *Get on the list.*
- Subhead: *We're onboarding design partners through Q3 2026. Tell us what you're building, get early access to the SDK and benchmark reports.*
- Large `<WaitlistInput />` (horizontal layout): email + role + company + `[ Join → ]`.

**S10 — Footer** (global).

**Motion notes for Landing:**
- Hero copy staggers in on page load with 60–120ms gaps.
- Each section below fades up on scroll-into-view, one time only.
- The Thesis and Pipeline animations are the hero moments. Other sections are quiet.

### 9.2 Product (`/product`)

**Purpose:** Explain *what the SDK is and what it does* for a technical reader who already knows they have a multi-agent pipeline and is evaluating solutions.

**Conversion anchor:** Primary CTA in hero and end-of-page.

**Sections, top to bottom:**

**S1 — Hero.**
- Overline: `• THE SDK`
- H1: **A drop-in Python SDK that optimizes inter-agent communication.**
- Subhead: *Wraps your existing orchestration. Agents, prompts, and model choices stay exactly as they are. Brevitas sits between agents and compresses, de-duplicates, and references instead of re-serializing.*
- No animation in hero — the code block IS the hero.
- Large `<CodeBlock />` as the primary visual:

```python
from brevitas import optimize

# Before
pipeline = [architect, builder, reviewer]
result = run_pipeline(pipeline, task)

# After
pipeline = optimize([architect, builder, reviewer])
result = run_pipeline(pipeline, task)
# ↳ 59% fewer tokens. 47% lower cost. 99% quality parity.
```

**S2 — What it is.** Prose section, single-column max-width 720px.
- H2: *A pipeline-level layer, not a per-call transform.*
- Body: Lays out the core positioning — sits *between* agents, not inside them. Explains why that distinction matters: composable with any orchestrator, agnostic to prompts and models, optimizes what single-call compressors and native caches cannot reach.

**S3 — What it composes with.** Three-row compatibility matrix.
- Row 1: **Orchestrators** — LangGraph · CrewAI · AutoGen · Custom
- Row 2: **Providers** — OpenAI · Anthropic · Google · DeepSeek · Groq · others via standard chat-completions API
- Row 3: **Runtimes** — Python 3.10+ · any environment the SDK can `pip install` into
- Each row has a small mono caption: *"We do not require a specific orchestrator. We do not require a specific provider. Pass in callables; get callables back."*

**S4 — What it does, in five moves.** A narrower, more technical version of the six-techniques grid from Landing. Each row has:
- The technique name (H3).
- A 4–6 line explanation.
- A tiny pseudo-code or schema illustration where applicable.
- No animations per row — these are reading material.

**S5 — Integration walkthrough.** Steps with code blocks.
- Install: `pip install brevitas`
- Wrap your pipeline
- Run as normal
- Inspect the optimization report (`pipeline.report()` — returns a dict of token counts, cost, and per-technique savings)

**S6 — What it doesn't do.** Editorial, direct.
- H2: *What Brevitas is not.*
- Bulleted editorial list:
  - *Not an orchestrator. Use yours.*
  - *Not a prompt compressor for single calls. (See The Token Company for that.)*
  - *Not a model router. (See Martian or OpenRouter.)*
  - *Not an observability tool. (See Helicone, Langfuse, Portkey.)*
  - *Not opinionated about your agents' prompts. They're yours.*
- This builds trust. Stating the non-goals honestly is the move.

**S7 — Final CTA.** Same pattern as Landing S9.

**S8 — Footer.**

### 9.3 How it works (`/how-it-works`)

**Purpose:** The deep dive. For the reader who is sold on the promise and wants to understand the mechanism. This page is what the reader sends to their CTO to get approval.

**Conversion anchor:** Light — a CTA at the end and in the footer. Don't interrupt the read.

**Sections, top to bottom:**

**S1 — Hero.**
- Overline: `• THE MECHANISM`
- H1: **Six techniques. One layer. Task-aware routing decides which to apply per call.**
- Subhead: *None of them are novel. The combination is. And no one has productized it for multi-agent pipelines.*
- Below: a condensed, looping version of the **Pipeline Reduction Animation** — no resolution frame, just the split-view running indefinitely. Acts as ambient context while the reader scrolls.

**S2 — The architecture.** The one-diagram version from the one-pager, rendered as an editorial SVG diagram (not ASCII). This is the spine of the page.

```
     [ Agent 1 ] ──▶ [ Brevitas ] ──▶ [ Agent 2 ] ──▶ [ Brevitas ] ──▶ [ Agent 3 ]
                         │                                 │
                         ▼                                 ▼
                 [ shared memory ]                 [ shared memory ]
                 [ compression  ]                  [ delta engine  ]
                 [ pruning      ]                  [ protocol enc  ]
```

- Use real SVG, not ASCII on the final site.
- The Brevitas boxes get subtle signal-green outline emphasis.
- On scroll, each technique label animates into place inside its Brevitas box with a 60ms stagger. One-time reveal.

**S3 — Technique 01: Inter-agent message compression.**
- H2 with numbered overline.
- 2–3 paragraphs of prose explaining mechanism.
- Inline **Compression Sequence** animation (§7.1) as the visual.
- Sub-paragraph: compression ratio controls (global, per-agent, per-task-class).

**S4 — Technique 02: Shared memory with content-addressed references.**
- H2, prose, small schematic (no motion needed — a static diagram showing "agent outputs stored once; downstream agents receive IDs, not re-serialized content").

**S5 — Technique 03: Delta mode.**
- H2, prose, small illustration showing "first call: full context. Subsequent calls: only changes."
- Analogy callout (pulled from the one-pager): *Analogous to how version control stopped sending full file copies.*

**S6 — Technique 04: Smart context pruning.**
- H2, prose. Explain relevance-pass mechanism, task-class awareness, per-agent-role rules.

**S7 — Technique 05: Compact message protocol.**
- H2, prose. Show a before/after schema example: free-form prose → structured schema.
- Tiny inline code block with an example schema.

**S8 — Technique 06: Task-aware routing.**
- H2, prose. Explain: not every call needs every optimization. The router picks a subset based on task class and pipeline shape.
- A small decision-tree-ish illustration is optional; OK to leave as prose if the designer would rather keep the page clean.

**S9 — Stacking and composition.**
- H2: *Why they're stackable.*
- Brief prose section closing the deep dive: the techniques are designed to compose. Applying all six is not always optimal; the router decides. Reductions in testing range from 20% to 70% depending on pipeline shape.

**S10 — Pointer to benchmarks.**
- Ghost CTA: `See the measured results →` → `/benchmarks`.

**S11 — Final CTA** (lighter than Landing's S9, a single line and an input).

**S12 — Footer.**

**Motion notes:**
- Each numbered section fades up on scroll-into-view.
- The ambient Pipeline animation in S1 is the only "hero" motion. Everything else is prose + static illustration.
- Technique 01's compression animation is the one exception — it's a legitimate explanatory visual.

### 9.4 Benchmarks (`/benchmarks`)

**Purpose:** Show the numbers, show the method, show the caveats. Build the defensible version of "does it work."

**Conversion anchor:** Bottom-of-page CTA.

**Sections:**

**S1 — Hero.**
- Overline: `• MEASURED RESULTS`
- H1: **59.4% fewer tokens. 46.9% lower cost. 99% quality parity.**
- Subhead: *Measured on peer-reviewed multi-agent benchmarks, with real API calls across DeepSeek, Groq, and OpenAI running simultaneously.*
- Three `<StatCard />` in `emphasis` variant, large.
- Caveat line in mono, `--stone-2`: *Current figures from n=1 AgentBench task. Full n≥50 suite results land in [TBD].*

**S2 — Method.**
- H2: *How we measured.*
- Narrow-column prose, ~300 words:
  - What the baseline is (full re-serialization — how every team builds this today).
  - What the optimized pipeline is (Brevitas layer between agents, all six techniques enabled, task-aware routing on).
  - The three-agent harness: Architect → Builder → Reviewer.
  - Provider assignment: DeepSeek (Architect), Groq Llama-3.3-70B (Builder), OpenAI GPT-4o-mini (Reviewer). Three providers simultaneously in one pipeline.
  - How quality was scored: manual inspection of output pairs, with AgentBench task-correctness in progress.

**S3 — The first-run table.** The table directly from the one-pager:

| Metric | Baseline pipeline | Optimized pipeline | Delta |
|---|---|---|---|
| Total tokens | 2,924 | 1,188 | **–59.4%** |
| Total cost | $0.001616 | $0.000857 | **–46.9%** |
| Output quality | Baseline reference | ~99% parity on inspection | **Negligible quality loss** |

Styled editorially, not as a dashboard table — tabular-num mono, ample row spacing, thin `--line` dividers, bold delta column.

**S4 — Benchmark cards.**
- Three cards, one per benchmark. Each card has:
  - `<BenchmarkBadge />` at the top.
  - Name + venue.
  - What the benchmark tests (1–2 lines).
  - Current status: "Integrated · Results pending full run" or "Integrated · n=1 result visible" etc.
- AgentBench, MARBLE, BattleAgentBench.

**S5 — Caveats and reproducibility.** Editorial section, ~200 words.
- What n=1 means. What a full run means.
- The reproducibility pledge: when the full suite runs, we publish the methodology and raw token counts.
- Invitation: *If you'd like to see the harness, we'll walk you through it — join the waitlist and ask.*

**S6 — Final CTA.**

**S7 — Footer.**

### 9.5 Docs (`/docs`) — stub

**Purpose:** Placeholder. Honest. *"Docs land with public SDK access. Join the waitlist to get early access."*

**Sections:**

**S1 — Hero.**
- Overline: `• DOCUMENTATION`
- H1: **Docs open with the SDK.**
- Subhead: *We're working with design partners through Q3 2026 to harden the API before publishing public docs. Join the waitlist for early-access documentation.*

**S2 — What's coming.** A narrow editorial list:
- *Python SDK reference*
- *Integration guides for LangGraph, CrewAI, AutoGen*
- *Task-class configuration cookbook*
- *Benchmark harness — run it yourself*
- *Provider notes (DeepSeek, Groq, OpenAI, Anthropic, Google)*

**S3 — CTA.** Large `<WaitlistInput />`.

**S4 — Footer.**

**Motion:** None beyond the standard scroll reveals.

### 9.6 Blog (`/blog`) — stub

**Purpose:** Index page + 1–2 founder-written posts at launch. Establishes the category narrative in long form and gives HN/Twitter something to link to.

**Index page sections:**

**S1 — Hero.**
- Overline: `• FIELD NOTES`
- H1: **Writing on multi-agent infrastructure, token economics, and the missing layer.**

**S2 — Post list.**
- Editorial layout. Each post entry is a full-width row with:
  - Date (mono, 13px)
  - Title (Newsreader 400, h3)
  - 2-line dek (Inter Tight, body-lg)
  - Author byline (mono, 13px)
  - Thin rule below
- No thumbnails. No tags. No "read time." This is writing, not a newsletter.

**Suggested launch posts (one-pager material is your raw feed):**

1. *"Multi-agent pipelines leak tokens exponentially. Here's the measurement."* — the one-pager's §2 expanded, with the n=1 table, the cumulative-token worked example, and an honest caveat about n≥50 landing soon.
2. *"Why 2026 is the right moment for inter-agent optimization."* — the one-pager's §5 expanded.

**Blog post template (`/blog/[slug]`):**

- Max-width 680px single column for prose.
- Newsreader for H1 and H2, Inter Tight for body, JetBrains Mono for inline code and data.
- Images (if any) full-width within the content column, 1px `--line` border, caption below in mono.
- Footnotes in `--stone-2` at the bottom. Superscript links inline.
- Footer of each post: byline card with author + date + "Share on X / LinkedIn / email" as text links, no icon buttons.

### 9.7 Waitlist (`/waitlist`)

**Purpose:** The conversion page. When someone hits "Join the waitlist" from anywhere else, they land here (except for inline forms on other pages that submit in place).

**Sections:**

**S1 — Hero.**
- Overline: `• WAITLIST`
- H1: **Get on the list.**
- Subhead: *We're onboarding design partners through Q3 2026. The waitlist is the path in.*

**S2 — The form.** See §10 for the full spec.

**S3 — What you get.** Small editorial list, below the form:
- *Early-access SDK, once stable.*
- *Design-partner benchmark reports (private, with raw token counts).*
- *A monthly note from the founder on the build. Fewer than 12 emails a year.*

**S4 — Footer.**

**Motion:** Minimal. The form is the point.

---

## 10. Waitlist — form, validation, backend, post-submit

### 10.1 Fields

The waitlist is a qualifier, not an open drip. Five fields. No more.

| # | Field | Type | Required | Purpose |
|---|---|---|---|---|
| 1 | Work email | email | yes | Primary identifier. Personal emails accepted; flagged but not rejected. |
| 2 | Name | text (short) | yes | First + last, one field. |
| 3 | Company | text (short) | yes | Manual text. No autocomplete against a domain list in v1. |
| 4 | Role | select | yes | One of: *Founder / CEO*, *CTO / Head of Engineering*, *Engineering IC*, *Product / Platform Lead*, *Investor*, *Researcher*, *Other*. |
| 5 | What are you building? | textarea (short) | yes, 20–300 chars | Qualification. This is the single most important field for Brevitas — it's how founder triage works. |

Optional field on the dedicated `/waitlist` page only (skip in inline/footer forms):

| 6 | How did you hear about us? | text, optional | no | Attribution |

### 10.2 Inline vs. dedicated variants

- **Inline `<WaitlistInput />`** (hero, footer, end-of-page): email only. One field + a submit button. On submit, expands into the remaining required fields inline. Progressive disclosure — don't scare off the low-intent visitor.
- **Dedicated `/waitlist` page:** full five-field form visible at once. The reader who arrived here by clicking "Join the waitlist →" has intent.

### 10.3 Validation

- Email: RFC-compliant regex + DNS MX lookup on the server. Reject clearly invalid emails with a single-line message: *"Hmm, that email doesn't look right."*
- Name: 2–80 chars, trimmed.
- Company: 2–100 chars, trimmed.
- Role: one of the enum above.
- "What are you building?": 20–300 chars. Under 20: *"A sentence or two helps us triage — what's the multi-agent use case?"* Over 300: just truncate with a counter.
- Everything validates on blur and on submit. No inline errors while the user is still typing.

### 10.4 States

**Default state:**
```
[ email@company.com                 ] [ Join the waitlist → ]
```

**After email submit (inline variant):**
```
[ email@company.com           ✓   ]

[ your name                      ]
[ company                        ]
[ role ▾                         ]
[ what are you building?         ]
[                                ]

                    [ Confirm and join → ]
```

**Submitting state:**
- Button disabled, `→` replaced with a tiny pulsing `·` dot.
- Inputs remain visible and readable; don't grey out the form.

**Success state:**
- Form collapses to a single card.
- Signal-green small check icon.
- Headline: *"You're on the list."*
- Body: *"We'll email at {domain} when we have room to talk. In the meantime, watch for a monthly note — no more, usually less."*
- Small secondary action: `[ Share with a teammate ]` → opens a share sheet with a pre-composed short message.

**Error state (server error or validation):**
- Inline message in `--bone-dim` with a subtle `!` glyph.
- One-line plain English. Never "Error 400." Never a stack trace. Never "Something went wrong, please try again."
- If a server error: *"Our waitlist is briefly unreachable. Try again in a minute?"*
- If already on the list: *"You're already on the list — look for a note from James in your inbox."* (No "please check your spam" — that's a signal of a low-deliverability system.)

### 10.5 Backend

Recommended approaches, in order of simplicity:

1. **Serverless function + airtable / notion / postgres** — write to a single waitlist table with the fields above plus `created_at`, `source_page`, `utm_*`, `user_agent`, `ip_country` (not IP itself — country only, for privacy hygiene). Airtable is fine for v1 at pre-seed scale.
2. **Resend or Loops for the confirmation email.** Send a simple plain-text-styled HTML email signed from James. No marketing template. No unsubscribe footer larger than 12px.
3. **Webhook to Slack** for founder notification on every submit. Slack message format:

```
New waitlist: {name} at {company}
Role: {role}
Building: {what_are_you_building}
Email: {email}
Source: {source_page}
```

4. **Rate limiting.** 3 submissions per IP per 10 minutes.
5. **Honeypot field** (`name=address_line_2`, hidden via CSS). Any submission with that field populated is dropped silently.
6. **No CAPTCHA in v1.** If spam becomes a problem, add Cloudflare Turnstile — not reCAPTCHA.

### 10.6 Privacy and compliance

- **Store only what's necessary.** Don't collect IP, don't collect browser fingerprint. Country-level inferred from IP, then discard the IP.
- **Cookie banner:** single, small, bottom-left, neutral. Three options: *Accept · Decline · Preferences*. No dark-pattern "Accept All" as the default focus. Respect the decision — if someone declines analytics, don't run analytics for them. This matters more for trust than for compliance.
- **Privacy page** (`/legal/privacy`): plain prose, no boilerplate template. 400–800 words. Written like a human wrote it.
- **Respect `prefers-reduced-motion` and `prefers-color-scheme`.** The site is dark-only in v1 (the deck has a light mode toggle; the web does not in v1). If the user has `prefers-color-scheme: light`, serve the dark site with a small tooltip or nothing at all — don't fight the user. Add a light mode in v2 if requested.

---

## 11. Copy library — every headline, subhead, and CTA on the site

Collected here so the writer, designer, and engineer all have one source of truth. Italic text inside a copy block is an editorial note, not copy to ship.

### 11.1 Global

- **Wordmark:** Brevitas Systems
- **Tagline (for og/twitter meta and brief mentions):** *The communication layer between your agents.*
- **Primary CTA:** `Join the waitlist →`
- **Secondary CTAs:** `See the benchmarks →` · `Read the deep dive →` · `Deep dive into each technique →` · `See full results →` · `See how →`

### 11.2 Landing

- **Hero overline:** `• INFRASTRUCTURE LAYER FOR MULTI-AGENT PIPELINES`
- **Hero H1:** *The communication layer between your agents.*
- **Hero subhead:** *Multi-agent pipelines lose 60% of their tokens to redundant inter-agent context. Brevitas gives it back — as a drop-in Python SDK, without touching your agents, prompts, or models.*
- **Hero stats footnote:** *First-run benchmark · AgentBench task · real API calls.*
- **Thesis overline:** `THE STATE OF THE STACK — 2026`
- **Thesis line 1:** *The 2023 default was a single prompt.*
- **Thesis line 1 caption:** `// gpt-4.turbo · one model · one call`
- **Thesis line 2:** *The 2026 default is a pipeline of agents.*
- **Thesis line 2 caption:** `// orchestrators · 5 to 50 inter-agent calls per task`
- **Thesis line 3 (mono intro):** `────────── and no one optimized`
- **Thesis line 3 (serif emphasis):** *what flows between them.*
- **Thesis line 3 caption:** `// until now`
- **Pipeline animation overline:** `THE PROBLEM, AND WHAT WE DO ABOUT IT`
- **Pipeline animation headline (below the animation):** *Same pipeline. Same task. 59% fewer tokens.*
- **Compression section H2:** *Every message between agents is compressed, referenced, or sent as a delta. Your agents don't change. The tokens between them drop by half.*
- **Techniques grid H2:** *One layer, six techniques. Task-aware routing decides which to apply per call.*
- **Integration section H2:** *Import. Wrap. Ship.*
- **Integration body:** *Brevitas wraps your existing multi-agent orchestration. Keep your agents. Keep your prompts. Keep your models. Drop-in compatible with LangGraph, CrewAI, AutoGen, and custom pipelines. Provider-agnostic — validated against DeepSeek, Groq, and OpenAI running simultaneously in a three-agent pipeline.*
- **Benchmarks overline:** `VALIDATED ON`
- **Benchmarks caption:** *Three peer-reviewed multi-agent benchmarks integrated into our test harness.*
- **Why now H2:** *Three inflection points make the first half of 2026 the right moment.*
- **Why now item 1 H3:** *Multi-agent crossed from demo to default.*
- **Why now item 2 H3:** *Cost accountability arrived.*
- **Why now item 3 H3:** *The tooling gap is visible — and closing.*
- **Final CTA H1:** *Get on the list.*
- **Final CTA subhead:** *We're onboarding design partners through Q3 2026. Tell us what you're building, get early access to the SDK and benchmark reports.*

### 11.3 Product

- **Hero H1:** *A drop-in Python SDK that optimizes inter-agent communication.*
- **Hero subhead:** *Wraps your existing orchestration. Agents, prompts, and model choices stay exactly as they are. Brevitas sits between agents and compresses, de-duplicates, and references instead of re-serializing.*
- **What it is H2:** *A pipeline-level layer, not a per-call transform.*
- **Composition H2:** *What Brevitas composes with.*
- **What it doesn't do H2:** *What Brevitas is not.*
  - *Not an orchestrator. Use yours.*
  - *Not a prompt compressor for single calls.*
  - *Not a model router.*
  - *Not an observability tool.*
  - *Not opinionated about your agents' prompts. They're yours.*

### 11.4 How it works

- **Hero H1:** *Six techniques. One layer. Task-aware routing decides which to apply per call.*
- **Hero subhead:** *None of them are novel. The combination is. And no one has productized it for multi-agent pipelines.*
- **Architecture H2:** *The architecture in one diagram.*
- **Technique H3 labels:**
  1. *Inter-agent message compression.*
  2. *Shared memory with content-addressed references.*
  3. *Delta mode.*
  4. *Smart context pruning.*
  5. *Compact message protocol.*
  6. *Task-aware routing.*
- **Closing H2:** *Why they stack.*
- **Ghost CTA:** `See the measured results →`

### 11.5 Benchmarks

- **Hero H1:** *59.4% fewer tokens. 46.9% lower cost. 99% quality parity.*
- **Hero subhead:** *Measured on peer-reviewed multi-agent benchmarks, with real API calls across DeepSeek, Groq, and OpenAI running simultaneously.*
- **Hero caveat:** *Current figures from n=1 AgentBench task. Full n≥50 suite results land in [TBD].*
- **Method H2:** *How we measured.*
- **Caveats H2:** *What n=1 means — and what we'll publish next.*

### 11.6 Docs

- **Hero H1:** *Docs open with the SDK.*
- **Hero subhead:** *We're working with design partners through Q3 2026 to harden the API before publishing public docs. Join the waitlist for early-access documentation.*

### 11.7 Waitlist

- **Hero H1:** *Get on the list.*
- **Hero subhead:** *We're onboarding design partners through Q3 2026. The waitlist is the path in.*
- **Form heading (above inputs):** *Tell us about what you're building.*
- **Placeholder (email):** `name@company.com`
- **Placeholder (textarea):** *e.g., "Three-agent research pipeline on GPT-4o and Claude 4.6 — ~10M tokens/month, cost the main constraint."*
- **Submit button:** `Confirm and join →`
- **Success H1:** *You're on the list.*
- **Success body:** *We'll email at {domain} when we have room to talk. In the meantime, watch for a monthly note — no more, usually less.*

### 11.8 404

- **H1:** *That page has been compressed out.*
- **Subhead:** *Nothing lives at this URL. Head back to the homepage or the waitlist.*
- **Two CTAs:** `← Home` · `Join the waitlist →`

### 11.9 Email confirmation (post-signup)

- **Subject:** *Brevitas Systems — you're on the list.*
- **From:** `james@brevitas.systems` (or actual founder address)
- **Body (plain, signed):**

> Thanks for joining the waitlist.
>
> Brevitas Systems is a small team building the optimization layer for multi-agent LLM pipelines. We're running design-partner conversations through Q3 2026 — if what you wrote looks like a fit, you'll hear from me directly.
>
> In the meantime, I'll send you a monthly note on what we're learning. Fewer than 12 emails a year. No marketing.
>
> — James

---

## 12. Technical stack — recommendations and constraints

### 12.1 Recommended stack

- **Framework:** Next.js 15 (App Router) on Vercel. Hydrates fast, is idiomatic for the design references, supports ISR for the blog.
- **Styling:** Tailwind CSS + a small CSS-variables layer imported directly from `theme.css` so tokens stay portable. (Specifically: import the existing `theme.css` root variables into the global stylesheet; use Tailwind's arbitrary-value syntax against those variables, e.g., `text-[var(--bone)]`.)
- **Fonts:** Self-host Newsreader (variable), Inter Tight, JetBrains Mono from Google Fonts → local files. `font-display: swap`. Preload the hero-critical weights.
- **Motion:** Framer Motion for the complex sequences (Thesis, Pipeline, Compression). Plain CSS transitions for micro-interactions. Do not pull in GSAP unless a specific animation requires it — Framer Motion is enough.
- **Icons:** Inline SVGs, per §4.5. Phosphor "thin" available if needed.
- **Forms:** React Hook Form + Zod for validation. Submit to a Next.js route handler that writes to Airtable (or whatever backing store is chosen) and posts a Slack webhook.
- **Blog/MDX:** Use MDX with `next-mdx-remote` or Contentlayer. Keep posts in `/content/blog/*.mdx` in the repo.
- **Analytics:** Plausible or Fathom — privacy-first, no cookie banner required for analytics itself. **Do not use GA4 in v1** — the cookie banner drag is worse than the analytics is worth at this stage.
- **Deployment:** Vercel. Preview deployments on every PR.

### 12.2 Constraints

- **Bundle size target:** First-load JS ≤ 120 KB gzipped on the landing page. Framer Motion is tree-shakeable; import only what's used.
- **Fonts:** total font payload ≤ 180 KB gzipped. That's tight with three families — keep subsetting aggressive (Latin only, only the weights specified in §4.2).
- **No third-party scripts on first load** beyond analytics. No chat widget. No "Capterra reviews" snippet. No Segment.
- **Images:** WebP with a JPG fallback. Hero animations are SVG or canvas, not video.
- **Server-rendered HTML on every page** — the marketing content should render without JS. Hydrate for interactions after.

### 12.3 Repository structure (suggested)

```
/
├── app/
│   ├── (marketing)/
│   │   ├── page.tsx                # Landing
│   │   ├── product/page.tsx
│   │   ├── how-it-works/page.tsx
│   │   ├── benchmarks/page.tsx
│   │   ├── docs/page.tsx
│   │   ├── blog/page.tsx
│   │   ├── blog/[slug]/page.tsx
│   │   └── waitlist/page.tsx
│   ├── (legal)/
│   │   ├── privacy/page.tsx
│   │   └── terms/page.tsx
│   ├── api/
│   │   └── waitlist/route.ts
│   ├── globals.css                 # imports theme.css vars
│   └── layout.tsx
├── components/
│   ├── nav/
│   ├── footer/
│   ├── animations/
│   │   ├── CompressionSequence.tsx
│   │   ├── PipelineReduction.tsx
│   │   └── ThesisSequence.tsx
│   ├── forms/
│   │   └── WaitlistInput.tsx
│   ├── marketing/
│   │   ├── StatCard.tsx
│   │   ├── TechniqueCard.tsx
│   │   ├── BenchmarkBadge.tsx
│   │   ├── SectionShell.tsx
│   │   └── Overline.tsx
│   └── ui/
│       ├── Button.tsx
│       └── CodeBlock.tsx
├── content/
│   └── blog/
├── public/
│   ├── fonts/
│   └── og/
├── lib/
│   ├── motion.ts                   # shared timings, easings
│   └── waitlist.ts
├── theme.css                       # copied from deck, single source of truth
└── package.json
```

---

## 13. Accessibility, performance, SEO

### 13.1 Accessibility (WCAG 2.2 AA as baseline)

- **Color contrast:** every text/background pair ≥ 4.5:1 for body, ≥ 3:1 for large text. The current `--bone` on `--ink` passes. `--bone-dim` on `--ink` should be checked — it's right around the line. `--stone-2` on `--ink` only for ≥ 14pt text.
- **Focus states:** visible, 2px offset ring in `--fg`, never suppressed. Primary CTAs get a bronze ring; everything else gets `--fg`.
- **Keyboard navigation:** every interactive element reachable by Tab, in document order. The waitlist form submits on Enter.
- **Screen reader:** all animations have `aria-hidden="true"` when decorative. Stat numbers have `aria-label` containing the final value (e.g., "negative 59.4 percent tokens saved") so the counter animation doesn't confuse SRs.
- **Reduced motion:** see §5.1 rule 4. Non-negotiable.
- **Skip to content** link at the top of every page for keyboard users.
- **Form labels** always visible (not placeholder-only). Use `<label>` elements linked via `htmlFor`, not `aria-label`.

### 13.2 Performance

- **Lighthouse targets:** Performance ≥ 90, Accessibility ≥ 95, Best Practices ≥ 95, SEO ≥ 95. Mobile scores are the ones that matter.
- **Largest Contentful Paint (LCP) < 1.5s** on Landing mobile over 4G.
- **Cumulative Layout Shift (CLS) ≈ 0.** Reserve space for every async-loaded element. The hero animations load into fixed containers with defined aspect ratios.
- **Time to Interactive < 3s** mobile 4G.
- **Run animations off-main-thread where possible** — use `transform` and `opacity` only for animation targets, never `width` / `height` / `top` / `left`.

### 13.3 SEO

- **Titles per page:**
  - `/` — *Brevitas Systems — The communication layer between your agents.*
  - `/product` — *Product · Brevitas Systems*
  - `/how-it-works` — *How it works · Brevitas Systems*
  - `/benchmarks` — *Benchmarks · Brevitas Systems*
  - `/docs` — *Docs · Brevitas Systems*
  - `/blog` — *Field notes · Brevitas Systems*
  - `/waitlist` — *Join the waitlist · Brevitas Systems*
- **Meta descriptions:** 150–160 chars each, hand-written, no templated suffix.
- **Open Graph:** every page gets a social image. For v1, a single branded OG image with the tagline is fine; blog posts get per-post OGs generated from the post title over the brand background.
- **Structured data:** `Organization` schema in the footer; `BlogPosting` schema on blog posts. Nothing more in v1.
- **Canonical URLs:** set on every page.
- **`robots.txt`:** allow everything. `sitemap.xml` auto-generated.

---

## 14. Analytics and experimentation

### 14.1 What to track in v1

- **Page views** per route, with referrer.
- **Waitlist conversions**, segmented by source (hero / footer / dedicated page / inline mid-page).
- **Scroll depth** on Landing — 25% / 50% / 75% / 100%. Only on Landing.
- **CTA clicks** on primary buttons (all `Join the waitlist →` instances).

Everything above is possible in Plausible or Fathom without cookies and without harming privacy. Event count will be small; do not build a data dashboard in v1.

### 14.2 What NOT to track

- **No heatmaps in v1.** Hotjar/Clarity pull in third-party JS and cookies for minimal pre-seed insight.
- **No session replay.**
- **No user-level attribution** beyond first-touch source of the waitlist signup.

### 14.3 A/B testing

Not in v1. Traffic volume doesn't justify statistical significance. Ship the best version you can articulate a defense for, and iterate on content as signal emerges from founder conversations with waitlist respondents.

---

## 15. Build phases and priority

Two-week build target for v1 assuming one designer + one engineer. Four-week target if engineer is also designer.

### Phase 1 — Foundations (days 1–3)

- Repo setup (Next.js, Tailwind, theme.css).
- Fonts loaded and verified.
- `<Nav />`, `<Footer />`, `<SectionShell />`, `<Button />`, `<Overline />` shipped.
- Global styles and motion primitives (`lib/motion.ts`) in place.
- Deployed to staging, working on mobile.

### Phase 2 — Landing (days 3–8)

- Landing sections S1 through S10 built.
- **Compression Sequence animation shipped** with reduced-motion variant.
- **Pipeline Reduction animation shipped** with reduced-motion variant.
- **Thesis Sequence shipped** with reduced-motion variant.
- Hero waitlist input working end-to-end (writes to backing store + Slack webhook).

### Phase 3 — Interior pages (days 8–12)

- `/product`, `/how-it-works`, `/benchmarks` built.
- `/docs` and `/blog` stubs.
- `/waitlist` full page.
- First blog post published.

### Phase 4 — Polish (days 12–14)

- Accessibility audit (axe DevTools, keyboard run-through).
- Lighthouse runs on every page; fix until targets hit.
- OG images generated.
- 404 page styled.
- Privacy/terms pages drafted.
- Launch.

### Explicitly deferred to v2

- Light mode.
- ROI calculator.
- Interactive playground.
- Pricing page.
- Changelog.
- Self-serve signup.
- Case studies (need design partners to live first).
- Multilingual support.
- Status page integration.

---

## 16. Open questions and founder decisions still needed

These are the points where the PRD makes an assumption that may be wrong. Resolve before or during build:

1. **Exact public SDK API shape.** The code blocks in this PRD use `optimize([architect, builder, reviewer])` as the illustrative pattern. If the real API is `Brevitas(agents=...).run(task)` or something else, every code block on the site updates to match. **Do not ship placeholder API code.**
2. **Brevitas domain.** `brevitas.systems` is assumed. If the real domain is `brevitas.ai`, `brevitas.dev`, or other, update every email, og, and footer reference.
3. **Founder name and author attribution.** The one-pager is authored by James; waitlist confirmation email is signed from James. Confirm this is the public-facing name (first + last optional).
4. **Benchmark numbers at launch.** v1 ships the n=1 59.4% / 46.9% / 99% figures with an explicit "n=1" caveat. If the n≥50 run lands before launch, swap in the stronger numbers and drop the caveat. **Do not launch without the caveat if the data isn't there yet** — investors read the fine print.
5. **Blog launch posts — draft or skip.** If neither of the two suggested posts is ready at launch, ship `/blog` with a single "coming soon" stub rather than an empty index.
6. **Design partner pipeline.** The final CTAs reference Q3 2026 onboarding. If that timeline changes, update Landing S9, `/docs` hero, `/waitlist` hero, and the email body.
7. **Status page.** "Status: operational" in the footer is visual signal today. If a real status endpoint exists by launch, wire it up; otherwise keep it as static text and flag this as a known-fake-until-real item in the roadmap.
8. **Advisor / mentor row.** The one-pager mentions possible named advisors (Algoverse / Berkeley mentor, Spur Accelerator). If any are willing to be named publicly before launch, add a small "advisors" strip to Landing between S7 (Benchmarks) and S8 (Why now). If none, skip — omitting is better than listing nobody.
9. **Logo row of design partners / customers.** None at launch, assumed. Explicitly skip — do not fabricate a "trusted by" row with placeholder logos. The three benchmark badges serve as the trust row until real customer logos exist.
10. **Light mode.** Deferred to v2 per §15. If the founder pushes for day-one light mode, add 1–2 days to the build estimate and extend all component styles.

---

## Appendix A — The three questions rule

Every page — and ideally every section — must service the three questions from the Problem One-Pager:

1. **Does it work?** → Benchmarks, numbers, methodology.
2. **Does it break my stack?** → Integration story, provider-agnostic positioning, "we don't touch your agents."
3. **Is it worth the switching cost?** → Integration time (minutes, not weeks), measurable ROI, honest caveats.

Use this as a review lens. Before shipping a section, check: which of the three questions does this service? If the answer is "none," the section is decoration and should be cut.

---

## Appendix B — Brevitas phrases and vocabulary

Use these phrases. They are the ones that work. Pulled from the one-pager, tightened for web:

**Do say:**
- *inter-agent communication*
- *pipeline-level layer, not a per-call transform*
- *drop-in Python SDK*
- *provider-agnostic by design*
- *token reduction* (not "cost savings")
- *quality parity* (not "quality improvement" — the claim is parity, which is honest and sufficient)
- *validated on peer-reviewed benchmarks*
- *keeps your agents, prompts, and models*
- *the communication layer between your agents*
- *the missing layer*

**Don't say:**
- *revolutionary, cutting-edge, next-gen, game-changing*
- *powered by AI* (we are AI; this phrase is meaningless)
- *unleash, unlock, supercharge*
- *seamless* (word is dead; use "drop-in" or describe the shape)
- *enterprise-grade* (unless we actually have enterprise customers, which we don't yet)
- *the future of X* (weak claim; show the present instead)

---

## Appendix C — Pointers to existing assets

- **Deck file:** `Brevitas_Systems.pptx` — the slide deck. Use as the visual authority alongside `theme.css`. Any decision not specified in this PRD should default to "match the deck."
- **Theme tokens:** `theme.css` in the remix bundle. This is the single source of truth for color and typography tokens.
- **Problem One-Pager:** `Problem_One_Pager.md`. The content authority. If copy on the site contradicts the one-pager, the one-pager wins unless James has explicitly revised.
- **Reference screenshot** (from the user): `Screenshot_2026-04-23_at_9_59_08_AM.png` — this is from The Token Company, the adjacent-but-not-competing single-call compressor. It is the **visual inspiration** for the Compression Sequence animation (§7.1) but not a template to copy one-for-one. Our version operates on inter-agent traces, not single prompts.

---

*End of PRD. Handing off to design/build. Changes requested inline — come back here for the next revision.*
