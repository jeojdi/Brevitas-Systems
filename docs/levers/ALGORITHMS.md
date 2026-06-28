# Exact algorithms (from primary sources)

Each algorithm below was extracted from the primary source (arXiv LaTeX, original PDF, or
RFC), not paraphrased from memory.

## Lever 1 — Provider-native caching

**Sources:** Anthropic prompt-caching docs; OpenAI prompt-caching docs.

- **Anthropic:** explicit `cache_control: {type:"ephemeral"}` breakpoints; minimum cacheable
  prefix **1024 tokens**; response `usage` reports `cache_read_input_tokens` (~0.1× input
  price) and `cache_creation_input_tokens` (~1.25×). Up to 4 breakpoints.
- **OpenAI/DeepSeek:** automatic caching for prefixes ≥1024 tokens (~0.5× on cached input);
  response `usage.prompt_tokens_details.cached_tokens`.

**Brevitas (`provider_cache.py`):** `apply_anthropic_cache` places breakpoints only where the
cumulative prefix (tools→system→prior turns) ≥ 1024 tokens, never the volatile tail, ≤4
breakpoints, nearest the tail. `savings_from_usage` computes honest input-side savings from
the real usage fields, including the turn-1 cache-write surcharge.

## Lever 2 — Content-addressed dedup

**IPFS (arXiv:1407.3561):** "content-addressed block storage … a generalized Merkle DAG."
The object name is the hash of its bytes; identical bytes → same name → one copy; references
are self-certifying (re-hash to verify).

**LBFS (SOSP 2001), verbatim:** "When the low-order **13 bits** of a region's fingerprint
equal a chosen value, the region constitutes a breakpoint. Expected chunk size 2^13 = **8 KB**
(plus the **48-byte** window)." Editing one region changes only the local chunk(s); all other
chunk hashes are unchanged.

**Brevitas (`content_store.py`):** SHA-256 CID (128-bit truncation); Rabin/Rabin-Karp rolling
fingerprint over a 48-byte window with a 13-bit boundary mask and min/max bounds; manifests
are Merkle nodes over child CIDs; `get_artifact` re-verifies every chunk and returns `None`
(fail-safe) on any missing/corrupt block.

## Lever 3 — Delta transmission

**Myers O(ND) (1986):** edit graph + greedy furthest-reaching D-paths = shortest edit script
(basis of UNIX `diff`). Implemented with per-D trace + backtrack.

**VCDIFF (RFC 3284):** delta = `COPY(addr,size)` / `ADD(bytes)` / `RUN(byte,size)` over the
superstring U = source S + target-so-far T; COPY may reference already-emitted target bytes.

**rsync (TR-CS-96-05, 1996):** weak rolling sum `a(k,l)=Σ X_i mod M`,
`b(k,l)=Σ(l−i+1)X_i mod M`, `s=a+2^16·b`, rolled one byte at a time, + a strong hash to
confirm a block match at any offset.

**Brevitas (`delta.py`):** Myers→VCDIFF ops for text/code; rsync block matching for large
blobs; `method="auto"`. Payload carries `base_hash`+`target_hash`; `apply_delta` rejects a
drifted base and re-hashes the reconstruction, returning `None` (→ full send) on mismatch.

## Lever 4 — Retrieval

**DPR (arXiv:2004.04906):** `sim(q,p)=E_Q(q)·E_P(p)`; top-k by maximum inner product;
trained with in-batch negatives + a BM25 hard negative (NLL loss).

**ColBERTv2 (arXiv:2112.01488):** late interaction `S_{q,d}=Σ_{i∈q} max_{j∈d}(q_i·d_j)`
(MaxSim); residual compression = nearest centroid + b-bit quantized residual (1–2 bits) →
6–10× smaller index.

**Brevitas (`retrieval.py`):** `DenseRetriever` (DPR MIPS, persisted embeddings); `maxsim`
operator; `ResidualCompressor` (configurable `nbits`, default 2); `fetch_for_hop` fail-safes
to full context on empty index / low top score.

## Lever 5 — Recursive Language Model

**RLM (arXiv:2512.24601), Algorithm 1 (verbatim):**

```
state <- InitREPL(prompt=P)
state <- AddFunction(state, sub_RLM_M)
hist  <- [ Metadata(state) ]
while True:
    code          <- LLM_M(hist)
    state, stdout <- REPL(state, code)
    hist <- hist || code || Metadata(stdout)
    if state[Final] is set: return state[Final]
```

Three load-bearing choices: (1) the model gets a symbolic handle to P (never copies it into
context); (2) output is built in the `Final` REPL variable (can exceed the window);
(3) symbolic recursion — code inside the REPL invokes the model over slices of P, with only
constant-size stdout metadata re-entering the root context.

**Brevitas (`rlm.py`):** `RLM.run(prompt, question)` runs the loop with a restricted-namespace
REPL exposing `P`, `sub_llm(prompt)`, `set_final(answer)`, `print`. Root context stays tiny
regardless of |P| (verified: needle in a 1M-char P with <5 KB root context).
