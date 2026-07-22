"""
Brevitas DeepSeek Benchmark
Measures response quality and token savings BEFORE vs AFTER Brevitas compression.
Models: deepseek-chat (V3) and deepseek-reasoner (R1)
Categories: logical_reasoning, deep_thinking, development, architect, reviewing
Quality: self-eval score (1-10) + reference-checklist diff
"""
import os, sys, time, json, textwrap
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

# ---------------------------------------------------------------------------
# Load key
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
env_path = ROOT / ".env.local"
for line in env_path.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

DEEPSEEK_KEY = os.environ.get("Deepseek_api_key", "")
if not DEEPSEEK_KEY:
    sys.exit("ERROR: Deepseek_api_key not found in .env.local")

from openai import OpenAI

# ---------------------------------------------------------------------------
# DeepSeek client (OpenAI-compatible)
# ---------------------------------------------------------------------------
ds = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")

# ---------------------------------------------------------------------------
# Brevitas pipeline
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT))
from token_efficiency_model.combined_tactics.pipeline import TokenEfficientPipeline
from token_efficiency_model.common.metrics import estimate_tokens, estimate_tokens_many

pipeline = TokenEfficientPipeline(model_backend=None, quality_floor=0.80, savings_target=20.0)

# ---------------------------------------------------------------------------
# Benchmark cases
# Each case has: multi-agent/multi-turn context (realistic repetition) + task
# Reference answers are a checklist of facts the correct answer MUST mention.
# ---------------------------------------------------------------------------
CASES = [
    # ── Logical Reasoning ──────────────────────────────────────────────────
    {
        "category": "logical_reasoning",
        "task": "Solve the logic puzzle and identify who owns the fish.",
        "prior_context": [
            "Agent 1 scanned the clues. There are five houses in a row.",
            "Agent 2 confirmed: five houses each painted a different colour.",
            "Agent 1 noted: each house owner has a different nationality.",
            "Agent 2 re-stated: each owner drinks a different beverage.",
            "Agent 1 added: each owner smokes a different brand of cigarettes.",
            "Agent 2 confirmed the beverage constraint from Agent 1's scan.",
            "Agent 1 also added: each owner keeps a different pet.",
            "Agent 2 repeated: each house has exactly one owner with one pet.",
        ],
        "messages": [
            "Agent 3 (Solver): I am solving the Einstein Fish Puzzle. "
            "Clue 1: The Brit lives in the red house. "
            "Clue 2: The Swede keeps dogs. "
            "Clue 3: The Dane drinks tea. "
            "Clue 4: The green house is left of the white house. "
            "Clue 5: The green house owner drinks coffee. "
            "Clue 6: The person who smokes Pall Mall has birds. "
            "Clue 7: The owner of the yellow house smokes Dunhill. "
            "Clue 8: The man in the centre house drinks milk. "
            "Clue 9: The Norwegian lives in the first house. "
            "Clue 10: The Blend smoker lives next to the cat owner. "
            "Clue 11: The horse owner lives next to the Dunhill smoker. "
            "Clue 12: The BlueMaster smoker drinks beer. "
            "Clue 13: The German smokes Prince. "
            "Clue 14: The Norwegian lives next to the blue house. "
            "Clue 15: The Blend smoker has a neighbour who drinks water. "
            "Agent 3 notes: Agent 1 and Agent 2 confirmed 5 houses, 5 owners, different pets, "
            "different beverages, different cigarettes as established in prior context. "
            "Now solve: who owns the fish?",
        ],
        "reference_facts": [
            "German", "fish", "house 4", "Prince"
        ],
    },
    {
        "category": "logical_reasoning",
        "task": "Given all premises, derive the valid conclusion using formal logic.",
        "prior_context": [
            "Agent A established the premises for the argument.",
            "Agent B re-confirmed: all men are mortal (universal statement).",
            "Agent A also stated: Socrates is a man.",
            "Agent B confirmed Agent A's premise: Socrates is indeed a man.",
            "Agent A noted the inference rule: modus ponens applies here.",
            "Agent B agreed that modus ponens is the right rule from Agent A's observation.",
        ],
        "messages": [
            "Agent C (Logician): Premise P1 (from Agent A and confirmed by Agent B): All men are mortal. "
            "Premise P2 (from Agent A, confirmed by Agent B): Socrates is a man. "
            "Applying modus ponens as noted by Agent A and agreed by Agent B. "
            "I now derive the conclusion. "
            "Additionally: If all ravens are black, and this bird is a raven, it is black. "
            "If p → q and p is true, then q must be true. "
            "What is the conclusion about Socrates, and what is the general logical rule demonstrated?",
        ],
        "reference_facts": [
            "Socrates is mortal", "modus ponens", "syllogism", "deductive"
        ],
    },

    # ── Deep Thinking ──────────────────────────────────────────────────────
    {
        "category": "deep_thinking",
        "task": "Analyse the philosophical implications of the ship of Theseus for software systems.",
        "prior_context": [
            "Agent 1 introduced the thought experiment: the ship of Theseus.",
            "Agent 2 summarised Agent 1's introduction: a ship whose planks are replaced one by one.",
            "Agent 1 added the software analogy: microservices replaced incrementally.",
            "Agent 2 restated Agent 1's analogy: software components swapped over time.",
            "Agent 1 noted: identity persistence is the philosophical question.",
            "Agent 2 confirmed identity persistence as the central question from Agent 1.",
        ],
        "messages": [
            "Agent 3 (Philosopher-Engineer): The ship of Theseus thought experiment (introduced by "
            "Agent 1 and summarised by Agent 2) asks: if every component is replaced, is it still "
            "the same entity? In software systems, as Agent 1 noted with microservices and Agent 2 "
            "confirmed, we face this daily: gradual rewrites, strangler fig patterns, blue-green "
            "deploys. Is a system rewritten service-by-service the same system? Analyse through: "
            "(1) functionalist identity — it does the same thing, "
            "(2) continuity identity — unbroken execution path, "
            "(3) data identity — same persisted state. "
            "What does this mean for versioning, SLAs, and system ownership?",
        ],
        "reference_facts": [
            "identity", "continuity", "functionalist", "versioning", "strangler"
        ],
    },
    {
        "category": "deep_thinking",
        "task": "Critically evaluate the CAP theorem trade-offs for a global financial system.",
        "prior_context": [
            "Agent 1 stated the CAP theorem: Consistency, Availability, Partition tolerance.",
            "Agent 2 confirmed Agent 1's statement of CAP theorem.",
            "Agent 1 added: you can only guarantee two of the three properties.",
            "Agent 2 restated: CAP says pick two from C, A, P.",
            "Agent 1 noted the context: global payments network, 99.999% uptime SLA.",
            "Agent 2 confirmed: the system must never lose a transaction.",
        ],
        "messages": [
            "Agent 3 (Architect-Thinker): The CAP theorem (Consistency, Availability, Partition "
            "tolerance — stated by Agent 1 and confirmed by Agent 2) says you can only guarantee "
            "two. For a global financial system with 99.999% uptime and zero lost transactions "
            "(context from Agent 1 and Agent 2): "
            "Should we choose CP (e.g., Spanner, strong consistency, sacrifice availability during "
            "partition) or AP (e.g., Cassandra, always available, eventual consistency)? "
            "Evaluate trade-offs including: regulatory compliance (ACID for ledgers), "
            "network partition probability across continents, compensation transactions (sagas), "
            "and the PACELC extension. What is the correct choice and why?",
        ],
        "reference_facts": [
            "CP", "partition", "consistency", "ACID", "saga", "PACELC"
        ],
    },

    # ── Development ────────────────────────────────────────────────────────
    {
        "category": "development",
        "task": "Write a Python async rate limiter using token bucket algorithm with Redis.",
        "prior_context": [
            "Agent 1 (Planner): Requirements gathered. Need async rate limiter.",
            "Agent 2 confirmed requirements from Agent 1: async, token bucket, Redis-backed.",
            "Agent 1 specified: 100 requests per minute per user, Redis 7 available.",
            "Agent 2 restated Agent 1's spec: 100 req/min/user, Redis 7 with async client.",
            "Agent 1 added: must be thread-safe and handle Redis connection failures gracefully.",
            "Agent 2 confirmed: thread-safe and graceful Redis failure from Agent 1's spec.",
        ],
        "messages": [
            "Agent 3 (Implementer): Based on Agent 1's requirements (async, token bucket, Redis 7, "
            "100 req/min/user, thread-safe) confirmed by Agent 2, I will now implement this. "
            "Use aioredis or redis.asyncio. Token bucket: each user has 100 tokens, refills at "
            "100/60 tokens per second. Use Redis MULTI/EXEC or Lua script for atomicity. "
            "Handle Redis down gracefully (fail open or closed, document the choice). "
            "Include type hints, async context manager support, and a usage example.",
        ],
        "reference_facts": [
            "async def", "token bucket", "redis", "lua", "rate_limit", "100"
        ],
    },
    {
        "category": "development",
        "task": "Implement a generic retry decorator in Python with exponential backoff and jitter.",
        "prior_context": [
            "Agent 1: User needs a retry utility for flaky network calls.",
            "Agent 2 confirmed Agent 1's need: retry with backoff for network calls.",
            "Agent 1 specified: exponential backoff, optional jitter, configurable max attempts.",
            "Agent 2 restated Agent 1's spec: exponential backoff + jitter, max_attempts param.",
            "Agent 1 added: should work as a decorator, support both sync and async functions.",
            "Agent 2 confirmed: decorator supporting sync and async from Agent 1.",
        ],
        "messages": [
            "Agent 3: Based on Agent 1's spec (exponential backoff, jitter, max attempts, "
            "sync+async decorator) confirmed by Agent 2, implement the retry decorator. "
            "Formula: wait = min(cap, base * 2^attempt) + random jitter. "
            "Support: configurable exceptions to retry on, on_retry callback, "
            "total timeout budget. Include Python 3.11+ type hints and a usage example.",
        ],
        "reference_facts": [
            "exponential", "jitter", "decorator", "async", "retry", "backoff"
        ],
    },

    # ── Architect ──────────────────────────────────────────────────────────
    {
        "category": "architect",
        "task": "Design a multi-tenant SaaS architecture for a data analytics platform serving 10,000 tenants.",
        "prior_context": [
            "Agent 1 (Requirements): 10,000 tenants, each with up to 1M rows of time-series data.",
            "Agent 2 confirmed Agent 1's scale: 10k tenants, 1M rows each.",
            "Agent 1 added: tenants must be isolated — no data leakage between them.",
            "Agent 2 restated: strong tenant isolation from Agent 1's requirement.",
            "Agent 1 specified: 99.9% uptime, sub-200ms P95 query latency.",
            "Agent 2 confirmed: 99.9% uptime, P95 < 200ms from Agent 1.",
            "Agent 1 noted: cost must scale linearly, not exponentially with tenant count.",
            "Agent 2 confirmed: linear cost scaling from Agent 1.",
        ],
        "messages": [
            "Agent 3 (Architect): Given the constraints from Agent 1 (10k tenants, 1M rows each, "
            "strong isolation, 99.9% uptime, P95 < 200ms, linear cost scaling) confirmed by Agent 2. "
            "Design the architecture. Cover: "
            "(1) tenant isolation strategy (schema-per-tenant vs row-level security vs separate DBs), "
            "(2) query routing and tenant context propagation, "
            "(3) storage layer (TimescaleDB? ClickHouse? Parquet on S3?), "
            "(4) caching strategy per tenant, "
            "(5) noisy-neighbour mitigation, "
            "(6) observability per tenant. "
            "Justify each choice with the given constraints.",
        ],
        "reference_facts": [
            "isolation", "schema", "row-level", "TimescaleDB OR ClickHouse OR Parquet",
            "cache", "noisy neighbour OR noisy-neighbour"
        ],
    },
    {
        "category": "architect",
        "task": "Design the event-driven architecture for a real-time fraud detection system.",
        "prior_context": [
            "Agent 1: System must detect fraud in under 100ms from transaction event.",
            "Agent 2 confirmed Agent 1's latency: under 100ms end-to-end.",
            "Agent 1 added: 50,000 transactions per second at peak.",
            "Agent 2 restated: 50k TPS from Agent 1's load requirement.",
            "Agent 1 noted: false positive rate must stay below 0.1%.",
            "Agent 2 confirmed: FPR < 0.1% from Agent 1.",
            "Agent 1 specified: system must explain why a transaction was flagged.",
            "Agent 2 restated: explainability required from Agent 1.",
        ],
        "messages": [
            "Agent 3 (Architect): Constraints (Agent 1, confirmed Agent 2): <100ms latency, "
            "50k TPS, FPR < 0.1%, explainability required. "
            "Design the event-driven fraud detection architecture. Cover: "
            "(1) ingestion layer (Kafka? Kinesis?) and partitioning strategy, "
            "(2) stream processing (Flink? Spark Streaming?), "
            "(3) model serving (feature store, online inference, model registry), "
            "(4) decision engine and explainability (SHAP? LIME?), "
            "(5) feedback loop for model retraining, "
            "(6) storage for audit trail and replay. "
            "How do you hit <100ms at 50k TPS?",
        ],
        "reference_facts": [
            "Kafka OR Kinesis", "Flink OR Spark", "feature store", "SHAP OR LIME OR explainability",
            "100ms", "partition"
        ],
    },

    # ── Reviewing ──────────────────────────────────────────────────────────
    {
        "category": "reviewing",
        "task": "Review this Python function for correctness, performance, and security issues.",
        "prior_context": [
            "Agent 1 flagged the function for review: user-supplied SQL query execution.",
            "Agent 2 confirmed Agent 1's concern: function executes user-supplied queries.",
            "Agent 1 noted: function is used in a public-facing API endpoint.",
            "Agent 2 restated: this runs in a public API from Agent 1's context.",
            "Agent 1 added: the database contains PII and financial records.",
            "Agent 2 confirmed: PII and financial data at risk from Agent 1's note.",
        ],
        "messages": [
            "Agent 3 (Reviewer): Context from Agent 1 and Agent 2: this function is called from a "
            "public API, executes user-supplied SQL, against a DB with PII and financial data. "
            "Review this code:\n"
            "```python\n"
            "def run_query(user_id: str, query: str, db_conn):\n"
            "    # Run a query for a user\n"
            "    result = db_conn.execute(f'SELECT * FROM data WHERE user_id={user_id} AND ({query})')\n"
            "    return result.fetchall()\n"
            "```\n"
            "Identify: SQL injection vulnerabilities, missing input validation, "
            "performance issues (no LIMIT), missing error handling, missing authorisation check. "
            "Provide the corrected version.",
        ],
        "reference_facts": [
            "SQL injection", "parameterized OR parameterised OR placeholder",
            "LIMIT", "authoriz OR authoris", "input validation"
        ],
    },
    {
        "category": "reviewing",
        "task": "Review this distributed locking implementation and identify race conditions.",
        "prior_context": [
            "Agent 1: Team implemented distributed lock using Redis SET NX.",
            "Agent 2 confirmed Agent 1's note: Redis SET NX is used for the lock.",
            "Agent 1 added: the lock is used to prevent duplicate payment processing.",
            "Agent 2 restated: prevents duplicate payments from Agent 1.",
            "Agent 1 warned: lock expiry and release need careful handling.",
            "Agent 2 confirmed Agent 1's warning about lock expiry.",
        ],
        "messages": [
            "Agent 3: Context (Agent 1 confirmed by Agent 2): Redis SET NX lock to prevent "
            "duplicate payments, careful lock expiry needed. Review:\n"
            "```python\n"
            "import redis, uuid\n"
            "r = redis.Redis()\n"
            "def acquire_lock(key: str, ttl: int = 30) -> str | None:\n"
            "    lock_id = str(uuid.uuid4())\n"
            "    if r.set(key, lock_id, nx=True, ex=ttl):\n"
            "        return lock_id\n"
            "    return None\n"
            "def release_lock(key: str, lock_id: str):\n"
            "    if r.get(key) == lock_id:  # check then delete\n"
            "        r.delete(key)\n"
            "```\n"
            "Find: TOCTOU race in release_lock (get then delete is not atomic), "
            "missing Lua script for atomic check-and-delete, lock extension not handled, "
            "no retry/backoff on acquire, what happens if process dies holding the lock.",
        ],
        "reference_facts": [
            "TOCTOU OR race condition", "atomic", "Lua", "TTL OR expire", "retry"
        ],
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def call_deepseek(model: str, system: str, user: str, timeout: int = 60) -> tuple[str, float]:
    t0 = time.time()
    resp = ds.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1500,
        temperature=0.2,
    )
    latency = time.time() - t0
    return resp.choices[0].message.content or "", latency


def self_eval_score(model: str, question: str, answer: str) -> float:
    prompt = (
        f"Rate this answer on a scale of 1-10 for correctness, completeness, and depth.\n\n"
        f"QUESTION:\n{question}\n\n"
        f"ANSWER:\n{answer}\n\n"
        f"Reply with ONLY a single integer 1-10. No explanation."
    )
    raw, _ = call_deepseek(model, "You are a strict technical evaluator.", prompt, timeout=30)
    for tok in raw.strip().split():
        try:
            v = float(tok.replace("/10", "").strip(".,"))
            if 1 <= v <= 10:
                return v
        except ValueError:
            continue
    return 5.0  # fallback


def reference_score(answer: str, facts: list[str]) -> float:
    """Fraction of required reference facts present in the answer (case-insensitive, OR groups)."""
    found = 0
    for fact in facts:
        alternatives = [a.strip() for a in fact.split(" OR ")]
        if any(alt.lower() in answer.lower() for alt in alternatives):
            found += 1
    return found / max(1, len(facts))


def compress_case(case: dict) -> tuple[list[str], list[str], int, int]:
    result = pipeline.process_task(
        task_text=case["task"],
        incoming_messages=case["messages"],
        prior_context=case["prior_context"],
        compression_level=2,
        prune_budget=4,
    )
    comp_msgs = result.debug.get("compressed_messages", case["messages"])
    pruned_ctx = result.debug.get("pruned_context", case["prior_context"])
    baseline = result.baseline_tokens
    out = estimate_tokens_many(comp_msgs) + estimate_tokens_many(pruned_ctx)
    return comp_msgs, pruned_ctx, baseline, out


def build_prompt(messages: list[str], prior_context: list[str], task: str) -> str:
    parts = []
    if prior_context:
        parts.append("PRIOR CONTEXT:\n" + "\n".join(f"- {c}" for c in prior_context))
    if messages:
        parts.append("AGENT MESSAGES:\n" + "\n\n".join(messages))
    parts.append(f"TASK:\n{task}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------
@dataclass
class Result:
    category: str
    task_short: str
    model: str
    mode: str
    baseline_tokens: int
    prompt_tokens: int
    savings_pct: float
    latency: float
    self_eval: float
    ref_score: float
    combined: float

results: list[Result] = []

MODELS = [
    ("deepseek-chat",     "V3"),
    ("deepseek-reasoner", "R1"),
]

print("\n" + "="*80)
print("  BREVITAS × DEEPSEEK BENCHMARK")
print("="*80)

for case in CASES:
    category = case["category"]
    task_short = case["task"][:55] + "…"
    ref_facts = case["reference_facts"]

    # Compress once, reuse for both models
    comp_msgs, pruned_ctx, baseline_toks, compressed_toks = compress_case(case)
    savings = round((1 - compressed_toks / max(1, baseline_toks)) * 100, 1)

    baseline_prompt = build_prompt(case["messages"], case["prior_context"], case["task"])
    compressed_prompt = build_prompt(comp_msgs, pruned_ctx, case["task"])

    print(f"\n[{category.upper()}] {task_short}")
    print(f"  Tokens: {baseline_toks} → {compressed_toks}  ({savings}% saved)")

    for ds_model, label in MODELS:
        for mode, prompt in [("BEFORE", baseline_prompt), ("AFTER", compressed_prompt)]:
            print(f"  {label} {mode}… ", end="", flush=True)
            try:
                answer, lat = call_deepseek(
                    ds_model,
                    "You are a senior technical expert. Answer thoroughly and precisely.",
                    prompt,
                )
                se = self_eval_score(ds_model, case["task"], answer)
                rs = reference_score(answer, ref_facts)
                combined = round((se / 10 * 0.5 + rs * 0.5) * 10, 2)  # 0-10 scale
                print(f"self-eval={se:.0f}/10  ref={rs:.0%}  combined={combined:.1f}/10  lat={lat:.1f}s")

                prompt_toks = estimate_tokens(prompt)
                results.append(Result(
                    category=category,
                    task_short=task_short,
                    model=label,
                    mode=mode,
                    baseline_tokens=baseline_toks,
                    prompt_tokens=prompt_toks,
                    savings_pct=savings if mode == "AFTER" else 0.0,
                    latency=lat,
                    self_eval=se,
                    ref_score=rs,
                    combined=combined,
                ))
            except Exception as e:
                print(f"ERROR: {e}")

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
print("\n" + "="*80)
print("  RESULTS SUMMARY")
print("="*80)

header = f"{'Category':<22} {'Model':<4} {'BEFORE':<26} {'AFTER':<26} {'Δ Quality':<10} {'Provider input avoided'}"
print(header)
print("-" * 100)

categories = list(dict.fromkeys(r.category for r in results))
for cat in categories:
    for _, label in MODELS:
        before = [r for r in results if r.category == cat and r.model == label and r.mode == "BEFORE"]
        after  = [r for r in results if r.category == cat and r.model == label and r.mode == "AFTER"]
        if not before or not after:
            continue
        b = before[0]; a = after[0]
        delta = a.combined - b.combined
        sign = "+" if delta >= 0 else ""
        toks_saved_pct = a.savings_pct
        print(
            f"{cat:<22} {label:<4} "
            f"se={b.self_eval:.0f}  ref={b.ref_score:.0%}  Q={b.combined:.1f}   "
            f"se={a.self_eval:.0f}  ref={a.ref_score:.0%}  Q={a.combined:.1f}   "
            f"{sign}{delta:.1f}/10     {toks_saved_pct:.1f}%"
        )

# Aggregate
print("\n" + "-"*100)
print("AGGREGATE (averaged across all cases):")
for _, label in MODELS:
    b_all = [r for r in results if r.model == label and r.mode == "BEFORE"]
    a_all = [r for r in results if r.model == label and r.mode == "AFTER"]
    if not b_all or not a_all:
        continue
    avg_b   = sum(r.combined for r in b_all) / len(b_all)
    avg_a   = sum(r.combined for r in a_all) / len(a_all)
    avg_sav = sum(r.savings_pct for r in a_all) / len(a_all)
    avg_lat_b = sum(r.latency for r in b_all) / len(b_all)
    avg_lat_a = sum(r.latency for r in a_all) / len(a_all)
    retention = avg_a / max(0.01, avg_b) * 100
    print(f"  {label}: Quality BEFORE={avg_b:.2f}/10  AFTER={avg_a:.2f}/10  "
          f"Retention={retention:.1f}%  Avg provider input avoided={avg_sav:.1f}%  "
          f"Latency Δ={avg_lat_a - avg_lat_b:+.1f}s")

# Per-category breakdown
print("\nPER-CATEGORY AVERAGES (both models combined):")
for cat in categories:
    b_cat = [r for r in results if r.category == cat and r.mode == "BEFORE"]
    a_cat = [r for r in results if r.category == cat and r.mode == "AFTER"]
    if not b_cat or not a_cat:
        continue
    avg_b = sum(r.combined for r in b_cat) / len(b_cat)
    avg_a = sum(r.combined for r in a_cat) / len(a_cat)
    avg_sav = sum(r.savings_pct for r in a_cat) / len(a_cat)
    delta = avg_a - avg_b
    sign = "+" if delta >= 0 else ""
    print(f"  {cat:<22} BEFORE={avg_b:.2f}  AFTER={avg_a:.2f}  Δ={sign}{delta:.2f}  Savings={avg_sav:.1f}%")

print("\n" + "="*80)
print("Benchmark complete.")

# Save raw results as JSON
out_path = ROOT / "benchmarks" / "deepseek_results.json"
out_path.parent.mkdir(exist_ok=True)
with open(out_path, "w") as f:
    json.dump([vars(r) for r in results], f, indent=2)
print(f"Raw results saved → {out_path}")
