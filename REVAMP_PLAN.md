# Brevitas Algorithm Revamp — Lossless Context + Native Caching

**Goal:** raise answer-quality retention from ~70% toward ~99% **while** keeping/raising cost savings, by replacing the lossy compression pipeline with the paper-backed approach: native prompt caching (lossless) + RLM context-as-variable retrieval (lossless) + real quality measurement.

**Source research:** see `.claude` memory `a2a-token-optimization-research`. Key papers: LLMLingua-2 (arXiv:2403.12968, demoted to opt-in), Recursive Language Models / RLM (arXiv:2512.24601), ColBERTv2 (NAACL 2022), Anthropic prompt caching (provider-native).

## Grounding rule (non-negotiable)

**Use published methods and their released implementations. Do NOT hand-roll algorithms or bespoke benchmarks unless strictly necessary.** Every optimization below maps to a paper + an existing library; the only net-new code is thin glue (provider adapters, orchestration loop, proxy plumbing). This REPLACES our current self-coded algorithms (`CommunicationCompressor`, `AdaptiveSemanticSampler`, `SmartContextPruner`, custom protocol/delta), which are the source of the quality loss.

| Concern | Method (paper) | Reuse — do not reimplement |
|---|---|---|
| Caching | provider-native | Anthropic `cache_control`; OpenAI/DeepSeek auto-cache SDK fields |
| Compression (opt-in) | LLMLingua-2 (arXiv:2403.12968) | `llmlingua` pip package + released model |
| Retrieval | ColBERT (NAACL 2022 / PyLate arXiv:2508.03555) | PyLate / RAGatouille / colbert-ai |
| Context-as-variable | RLM (arXiv:2512.24601) | reference impl github.com/alexzhang13/rlm |
| Quality eval | LLM-as-judge + embedding sim (established) | standard public datasets + existing eval libs — no bespoke benchmark |

Net-new code allowed only where no library exists: per-provider cache adapters, RLM tool-call orchestration loop, proxy upstream routing.

---

## Diagnosis (current state)

| Component | File | Problem |
|---|---|---|
| Message compression | `token_efficiency_model/agent_communication_compression/compressor.py` | **Lossy** — drops near-duplicate sentences by lexical overlap. Can drop critical facts. |
| Semantic sampling + pruning | `adaptive_semantic_sampling/sampler.py`, `smart_context_pruning/` | **Lossy** — keeps only top-N contexts on Jaccard/keyword heuristics. Root cause of context loss → 70%. |
| Quality gate | `common/metrics.py:quality_proxy_score` | **Fake** — heuristic formula, not real accuracy. Can't trust the 70% or the 0.98 floor. |
| Token counting | `common/metrics.py:estimate_tokens` | **Fake** — `words × 1.3`, not a real tokenizer. Savings numbers are estimates. |
| Anthropic backend | `api/server.py:_make_anthropic_backend` | Flattens protocol+context into ONE user message, `max_tokens=1024`, **no `cache_control`, no system/tools split** → zero prompt caching. Biggest missed lossless win. |
| Backend interface | `pipeline.py` → `prompt = f"{protocol_payload}\nINLINE_CONTEXT={inline_chunks}"` (~L337); `model_backend(prompt, model)->str` | Flat-string interface makes structured caching impossible. Central blocker. |

**Thesis:** lossy compression has a quality ceiling (~70–90%). To reach ~99% you must switch the *mechanism* to lossless: retain all context, send it cheaply (cache), fetch it precisely (retrieval). Keep the existing compressor only as an opt-in "max savings" mode behind a real quality gate.

---

## Phase 0 — Instrument truth (prerequisite; ~2–3 days)

You cannot fix a 70% you cannot measure. Replace fake metrics with real ones.

1. **Real token counts.** Replace `estimate_tokens` usage on the billing path with provider-reported counts:
   - Anthropic response `usage`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.
   - For baseline (what they *would* have paid uncached), use Anthropic `POST /v1/messages/count_tokens`.
   - Keep `estimate_tokens` only as a pre-flight estimate, never as the billed number.
2. **Real quality eval harness.** New module `token_efficiency_model/eval/`:
   - A held-out set of representative (task, full_context, reference_answer) cases.
   - `judge.py`: LLM-as-judge (cheap model) + embedding cosine similarity of optimized-answer vs full-context-answer. Output a 0–1 retention score.
   - CLI: `python -m token_efficiency_model.eval.run --pipeline current` → prints real retention %. **Run this first to confirm the true number** (likely ≠ 70).
3. **Acceptance:** dashboard shows real token usage per request; eval harness prints a real, reproducible retention score for the current pipeline.

---

## Phase 1 — Native provider caching, MULTI-PROVIDER (lossless; biggest fast win; ~1–2 weeks)

This is the ~40–55% cost cut with **zero** quality risk. Caching is **provider-specific** — two of our four providers cache automatically, and our current compression likely *breaks* them.

### Provider caching matrix
| Provider | Upstream | Caching | Brevitas action |
|---|---|---|---|
| Anthropic | api.anthropic.com | **Explicit** `cache_control` breakpoints, 5-min ephemeral | Inject ≤4 breakpoints on stable prefix (min ~1024/2048 tok) |
| OpenAI | api.openai.com | **Automatic** longest-prefix cache >1024 tok, ~50% off cached input | Preserve prefix stability; no injection |
| DeepSeek | api.deepseek.com | **Automatic** disk context cache, hits ~10× cheaper | Preserve prefix stability; no injection |
| Groq | api.groq.com | **None** | No cache lever — use routing + RLM retrieval |

### ⚠️ Universal principle (fixes a current regression)
OpenAI & DeepSeek discount the repeated prefix **only if it is byte-identical across calls**. The current per-turn lossy compressor **mutates the prefix and destroys these free caches** → we may be paying full price while believing compression saved money. **Rule: never compress/reorder the stable prefix (system + tools + prior turns); only compress the volatile tail (the newest message).** Measure this regression explicitly in Phase 0.

### Proxy upstream routing gap
`brevitas/proxy.py` hardcodes the OpenAI path to `api.openai.com` → it cannot reach DeepSeek/Groq. Add base-URL routing by model prefix or header (port `_PROVIDER_BASE_URLS` from `api/server.py`).

### 1a. Refactor the model-backend interface (central enabler)
- Change `model_backend` from `(prompt: str, model: str) -> str` to a structured contract:
  ```python
  # request: {system: [...blocks], tools: [...], messages: [...], max_tokens, model}
  # response: {text, usage}
  def backend(request: ModelRequest) -> ModelResponse: ...
  ```
- In `pipeline.py`, stop building `prompt = f"{payload}\nINLINE_CONTEXT=..."`. Instead emit a structured `ModelRequest`: system block (instructions/protocol), optional tools, and `messages` (the actual conversation/context). Update all backends in `api/server.py` (`_make_anthropic_backend`, `_make_openai_compat_backend`, `_make_ollama_backend`) to the new contract.

### 1b. Per-provider cache adapters
- New `token_efficiency_model/optimizers/provider_cache/` with one adapter per provider behind a shared interface `apply_cache(request) -> request`:
  - **anthropic.py** — insert `cache_control: {"type": "ephemeral"}` at the END of: (1) tool defs, (2) system block, (3) stable conversation prefix. Guardrails: ≤4 breakpoints; skip blocks below min cacheable length (~1024 Sonnet/Opus, ~2048 Haiku); never mark the volatile tail.
  - **openai.py / deepseek.py** — no injection; **enforce prefix stability**: keep system+tools+prior turns first and untouched, compress only the newest message. (DeepSeek is OpenAI-schema; shares the adapter.)
  - **groq.py** — no-op for caching.
- Fix `brevitas/proxy.py`: route OpenAI-path upstreams to the correct base URL per provider (OpenAI/DeepSeek/Groq), don't hardcode `api.openai.com`.
- Set realistic `max_tokens` (current hardcoded 1024 in `api/server.py:_make_anthropic_backend` truncates real answers — bug).

### 1c. Meter & attribute (per provider)
- Anthropic: read `cache_creation_input_tokens` / `cache_read_input_tokens` from `usage`. OpenAI: read `usage.prompt_tokens_details.cached_tokens`. DeepSeek: read `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
- Compute real `baseline` vs `actual` per provider's cache pricing; log to usage store tagged `optimizer=native_cache`, `provider=...`.

### 1d. Acceptance (all four providers)
- Replay a 5-turn conversation per provider: cached-token field > 0 on turn ≥2 (Anthropic/OpenAI/DeepSeek); logged cost drops ≥40%; **output byte-identical to the un-cached request** (proves losslessness). Confirm OpenAI/DeepSeek prefix-stability fix recovers cache hits the old compressor was destroying. Default **ON**.

---

## Phase 2 — Lossless context via RLM retrieval (the 70%→~99% fix; ~2–3 weeks)

Replace the default lossy sampler/pruner path with retain-all + fetch-precisely. Your `SharedMemoryLayer` is already a primitive of this (snapshots, refs, delta) — upgrade it into a real RLM context store.

1. **Context store.** Store the FULL `prior_context` externally (hash-keyed; Redis/object store; dedupe across turns). Do not pre-prune it.
2. **Retrieval index.** Index stored context with embeddings (Phase 2a) and optionally **ColBERT late-interaction** (Phase 2b) for high-fidelity span selection. Replace the Jaccard/keyword `AdaptiveSemanticSampler` on the default path.
3. **RLM depth-1 loop (all providers with tool-calling).** Give the model a `fetch_context(query)` tool over the full store and run a tool-use loop: the model pulls only the snippets it needs, then answers. Nothing is discarded → near-lossless; also mitigates context rot, so quality can exceed the dump-everything baseline. **Tool schemas differ per provider:** OpenAI + DeepSeek + Groq share the OpenAI `tools` schema; Anthropic uses its own `tools` format. Implement one `fetch_context` tool, two schema serializers.
4. **Demote lossy compression.** Move `CommunicationCompressor` + aggressive pruning behind an opt-in `mode="max_savings"` flag, always quality-gated (Phase 3).
5. **Acceptance:** on the Phase 0 eval set, retention ≥95% AND token savings ≥ current, vs the lossy baseline. Shadow-eval on a traffic sample before enabling per customer.

---

## Phase 3 — Real quality gate wired to billing (~3–5 days)

1. Replace `quality_proxy_score` with the Phase 0 judge/similarity gate on the live path.
2. On any optimized response below the per-customer quality floor → **fall back to full context** (rehydrate) and retry. Better to save 0% than ship a wrong answer.
3. **Bill only savings that pass the gate** (aligns the %-of-savings model; kills the over-optimize→churn incentive).
4. Record real token usage + real retention score per request in the usage store; surface both in the dashboard.

---

## Phase 4 — Reuse & polish (ongoing)

- **Homogeneous sub-fleets:** add DroidSpeak/KV-reuse (arXiv:2411.02820) where customers run same-base models — Tier-2 latency win.
- **Tiered modes:** `lossless` (default: cache + RLM retrieval), `balanced`, `max_savings` (opt-in lossy).
- **Self-host packaging:** keep the gateway container cloud-agnostic (already FastAPI + Docker + railway.json).

---

## Sequencing & ownership

1. Phase 0 (truth) → 2. Phase 1 (caching, ship + measure) → 3. Phase 2 (lossless context) → 4. Phase 3 (gate+billing) → 5. Phase 4.

**Ship Phase 1 first** — it's lossless, fast, ~40–55% cost cut, and de-risks the whole effort by proving real measurement before touching the accuracy-critical context path.

## Files that change
- `api/server.py` — backend contract, Anthropic structured request + caching, real-usage metering.
- `token_efficiency_model/combined_tactics/pipeline.py` — emit structured `ModelRequest`; default path → retrieval not pruning.
- `token_efficiency_model/common/metrics.py` — real token counts; remove `quality_proxy_score` from live path.
- NEW `token_efficiency_model/optimizers/anthropic_prompt_cache.py`, `token_efficiency_model/eval/`, `token_efficiency_model/context_store/` (RLM).
- `token_efficiency_model/shared_memory_layer/` — upgrade to RLM context store.
- Lossy modules (`agent_communication_compression/`, `smart_context_pruning/`, `adaptive_semantic_sampling/`) — keep, gate behind `max_savings`.
