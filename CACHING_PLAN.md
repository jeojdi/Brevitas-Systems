# Brevitas Caching Plan — Semantic Cache + Cross-Provider Prompt Caching

**Goal:** Make Brevitas actually save money via caching (the real lever), not just
lossless compression (~0% on prose). Two parts, in order:

1. **Semantic response cache** — skip the model call entirely when the same/similar
   request has already been answered (100% savings on a hit). *Built first.*
2. **Cross-provider prompt caching** — correctly trigger + measure + bill each
   provider's native prompt cache, for all major providers.

Both wired into the **proxy** (`brevitas/proxy.py`), which is where requests are
intercepted.

---

## Locked decisions (from product owner)

| Decision | Choice |
|---|---|
| Cross-model reuse | **Same model only.** `model_id` is part of the exact cache key. A cached answer is never served to a different model. (Guarded cross-model tier is a *later, optional* step, not this phase.) |
| Cache safety posture | **Conservative.** Only cache low/zero-temperature, non-streaming, no-tools, 2xx responses. High similarity threshold. Near-zero false hits. |
| Embeddings | **Local model** (`bge-small`, CPU, ~130MB, no per-call cost, prompts never leave infra). Optional dependency; if absent, semantic layer disables gracefully and the hash layer still runs. |
| Provider scope | **All majors**, staged: harden the 4 already-routed (Anthropic/OpenAI/DeepSeek/Groq) → add easy OpenAI-compatible ones (Mistral, xAI) → add heavy-format ones (Gemini, Bedrock, Azure). |

---

## What already exists (DO NOT rebuild)

- `token_efficiency_model/lossless/provider_cache.py`
  - `apply_anthropic_cache(body)` — mature `cache_control` breakpoint injection with
    per-model minimums + safety margin. **Keep as-is.**
  - `savings_from_usage(usage, provider, model)` — reads REAL cache-hit tokens from a
    provider `usage` object for anthropic/openai/deepseek/groq and computes honest
    savings incl. output. **Already correct — just not wired into the proxy.**
- `brevitas/proxy.py` — Anthropic endpoint already calls `apply_anthropic_cache`.
  OpenAI-compatible endpoint routes OpenAI/DeepSeek/Groq but does **no** cache work.
- `api/store.py` — `UsageStore` (SQLite) + `SupabaseUsageStore`, selected by
  `make_store()`. `PROVIDER_COSTS_PER_1M` pricing table.

**Implication:** Part 2 is mostly *wiring existing code*, not new algorithms.

---

## PART 1 — Semantic response cache (build first)

### 1.1 New module `brevitas/semantic_cache.py`
`SemanticCache` with:
- `cacheable(body, provider) -> bool` — conservative gate: temperature ≤ 0.2
  (semantic layer); no `tools`; not `stream`. (Thresholds in config, tunable.)
- `key(body, provider, model) -> str` — SHA-256 over the parts that MUST match
  exactly (research §4): `model_id, system, temperature, top_p, tools,
  messages[:-1], max_tokens`. Only the **last user message** is left for semantic
  matching. `model_id` is always in the key ⇒ same-model-only for free.
- `lookup(body, provider, model) -> CachedResponse | None`
  1. **Layer 1 — exact hash:** SQL lookup by `key`. Sub-ms, no embedding. Catches
     identical repeats (retries, loops, parallel agents with same context).
  2. **Layer 2 — semantic:** only on Layer-1 miss and if cacheable. Embed the last
     user message locally, nearest-neighbor within the same `model_id`, return if
     cosine ≥ threshold (default **0.97**, conservative) and not expired.
- `store(body, provider, model, response, usage)` — write response + embedding +
  `expires_at` (TTL with ±jitter) + `version`. Only 2xx.

Backends (same logic, swappable):
- **Default (local proxy): SQLite** file next to `api/brevitas.db`. Nearest-neighbor
  = brute-force cosine in Python over non-expired rows for that model.
  `# ponytail: O(n) scan, fine to ~10k rows; swap for pgvector when hosted/large`.
- **Hosted: Supabase + pgvector** (see migration 002) when service-role creds are
  present — mirrors `make_store()`. `<=>` cosine query instead of Python scan.

**Sharing boundary (be explicit to users):** SQLite backend shares the cache across
every agent pointing at that one local proxy, and survives restarts — but not across
machines. Cross-machine sharing = hosted/Supabase backend.

### 1.2 New module `brevitas/_embed.py`
Lazy loader for `sentence-transformers` `BAAI/bge-small-en-v1.5`. Import inside the
function; on `ImportError` return `None` so the semantic layer silently disables and
the hash layer keeps working. Model loaded once (module-level singleton).

### 1.3 Dependency
`pyproject.toml`: add optional extra
`semanticcache = ["sentence-transformers>=3", "numpy"]`. Base install unaffected.

### 1.4 Wire into proxy (`brevitas/proxy.py`), BOTH endpoints
Before forwarding: `hit = cache.lookup(...)`; if hit → return a provider-shaped
response built from the stored text (Anthropic `content`/OpenAI `choices` shape),
report **100% savings** for that call, skip the upstream entirely.
After a miss+successful upstream call: `cache.store(...)`.

### 1.5 Billing on a semantic hit
100% skip = full cost saved. Use the stored response's token counts to compute
`cost_saved_usd` (input+output at the model's price). Tag the usage row
`cache_hit='semantic'|'exact'`.

---

## PART 2 — Cross-provider prompt caching (all majors, staged)

Grounded in `scratchpad/research_provider_caching.md`. Split: **3 providers need us
to inject markers** (Anthropic, Mistral, Bedrock); **6 cache automatically** and only
need us to *read `usage` and bill* (OpenAI, DeepSeek, Gemini, Azure, Groq, xAI).

### Phase 2a — Wire what already exists (highest ROI, smallest diff)
- In **both** proxy endpoints, after the upstream response, call
  `savings_from_usage(usage, provider, model)` and report the **real cache savings**
  (today the proxy reports compression savings only → auto-cache hits from
  OpenAI/DeepSeek/Groq are invisible & unbilled).
- **Streaming:** capture the final SSE `usage` chunk (tee the stream) so cache hits
  on streamed calls are measured too.
- Anthropic: already injects `cache_control`; switch its billing to
  `savings_from_usage` from the response `usage`.

### Phase 2b — Easy OpenAI-compatible providers
- **Mistral:** add upstream `https://api.mistral.ai/v1`, route `mistral-*`, inject
  `prompt_cache_key` (stable per prefix/session). Add to SSRF allowlist.
- **xAI Grok — FIX ROUTING BUG:** today `grok-*` is sent to Groq's API
  (`get_openai_compatible_upstream`). Add upstream `https://api.x.ai/v1`, route
  `grok-*` → xAI, keep Groq for `groq-*`/explicit. Add `prompt_cache_key`/conv-id hint.
- Add `prompt_cache_key` routing hints for OpenAI/Azure/Groq (improves hit rate; a
  stable hash of the request prefix).
- Add pricing rows (`store.py PROVIDER_COSTS_PER_1M`) + rate ratios
  (`provider_cache.py _RATES/_MODEL_RATES`) for Mistral/xAI.

### Phase 2c — Heavy formats (separate endpoints/auth; land last)
- **Azure OpenAI:** deployment-URL + `api-key` header; largely OpenAI-shaped (lighter).
- **Google Gemini:** new endpoint translating to/from `generateContent`; read
  `usageMetadata.cached_content_token_count`. (Different request/response shape.)
- **AWS Bedrock (Claude):** SigV4 auth + Converse API; reuse the Anthropic
  `cache_control` injector. (AWS signing is the main new work.)

> ⚠ Verify before shipping 2c pricing (research flagged UNVERIFIED): Gemini storage
> cost, DeepSeek min tokens, xAI/Azure discount %. Confirm on official pricing pages.

---

## Billing & observability
- `usage_log`: add `cache_hit TEXT DEFAULT ''` (`''|exact|semantic|provider`) and
  `cached_tokens INT DEFAULT 0`. SQLite auto-migrate (existing pattern in `store.py`)
  + Supabase migration.
- Don't double-count: a semantic hit reports 100% and makes **no** upstream call;
  compression + provider-cache savings only apply on real calls.

## Migrations
- `api/migrations/002_semantic_cache.sql` — `create extension vector`, `semantic_cache`
  table + ivfflat index + `semantic_cache_lookup()` fn (hosted backend), and the two
  new `usage_log` columns. (SQLite path creates its cache table in code.)

## Tests (one runnable self-check per non-trivial unit — ponytail rule)
- `semantic_cache`: exact hit, semantic hit ≥ threshold, miss < threshold, **model
  isolation** (same text, different model ⇒ miss), temp/tools/stream gate, TTL expiry.
- `provider_cache.savings_from_usage`: feed synthetic `usage` dicts per provider,
  assert savings math (extend existing).
- proxy: mocked upstream — assert (a) semantic hit short-circuits upstream, (b) miss
  forwards + stores, (c) `savings_from_usage` billed on a miss.

## Build order for auto-mode
1. Part 1 (semantic cache: `_embed.py`, `semantic_cache.py`, dep, SQLite backend, tests)
2. Part 1 wiring into both proxy endpoints + hit billing
3. Phase 2a (wire `savings_from_usage` + streaming usage capture)
4. Phase 2b (Mistral, xAI fix, prompt_cache_key hints, pricing rows)
5. Migration 002 + usage_log columns + hosted pgvector backend
6. Phase 2c (Azure → Gemini → Bedrock), each behind its own step
7. `/verify` end-to-end against a live low-cost provider (DeepSeek) after 1–3

## Explicitly out of scope (say so, don't silently skip)
- Caching in the SDK `wrap()` path (proxy only, per instruction). Easy follow-up.
- Guarded cross-model cache sharing (deferred; needs a quality gate).
- Dashboard UI for cache-hit savings (data will be there; UI is a follow-up).
