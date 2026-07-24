"""
Brevitas × DeepSeek Ground-Truth Benchmark
===========================================
Datasets: real public benchmark datasets with known correct answers.
Scoring:  NO self-eval. Pure accuracy vs ground truth.
Models:   deepseek-chat (V3) and deepseek-reasoner (R1)
Modes:    BEFORE (full multi-agent context) vs AFTER (Brevitas-compressed)

Benchmarks used:
  logical_reasoning  → ARC-Challenge (ai2_arc)
  deep_thinking      → BBH logical_deduction_five_objects (lukaemon/bbh)
  development        → HumanEval pass@1 (openai_humaneval)
  architect          → MMLU college_computer_science + machine_learning (cais/mmlu)
  reviewing          → MMLU formal_logic (closest standard; no dedicated code-review benchmark exists)

Published baselines (DeepSeek technical reports, standard prompting):
  V3: MMLU=88.5%, HumanEval=82.6%, BBH=87.5%
  R1: MMLU=90.8%, HumanEval=92.9%, BBH=83.9%, MATH-500=97.3%
"""

import os, sys, re, json, time, random, subprocess, tempfile
from pathlib import Path
from dataclasses import dataclass, asdict

# ---------------------------------------------------------------------------
# Env + keys
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
for line in (ROOT / ".env.local").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

DEEPSEEK_KEY = os.environ.get("Deepseek_api_key", "")
if not DEEPSEEK_KEY:
    sys.exit("ERROR: Deepseek_api_key not found in .env.local")

from datasets import load_dataset
from openai import OpenAI

sys.path.insert(0, str(ROOT))
from token_efficiency_model.combined_tactics.pipeline import TokenEfficientPipeline
from token_efficiency_model.common.metrics import estimate_tokens, estimate_tokens_many
from brevitas.resource_bounds import safe_close_resource

ds_client = None
brevitas  = TokenEfficientPipeline(model_backend=None, quality_floor=0.80, savings_target=20.0)

MODELS    = [("deepseek-chat", "V3"), ("deepseek-reasoner", "R1")]
N_SAMPLES = 15   # per benchmark (× 2 models × 2 modes = 60 API calls per benchmark)
SEED      = 42
random.seed(SEED)

LETTERS = ["A", "B", "C", "D", "E"]

# ---------------------------------------------------------------------------
# API call — temperature=0 for reproducibility
# ---------------------------------------------------------------------------
def call_ds(model: str, system: str, user: str, max_tokens: int = 1024) -> tuple[str, float]:
    t0 = time.time()
    r = ds_client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return (r.choices[0].message.content or "").strip(), time.time() - t0

# ---------------------------------------------------------------------------
# Multi-agent pipeline wrapper
# Simulates the realistic repetition that occurs in multi-hop agent pipelines.
# Brevitas compresses the cross-agent redundancy; the final solver message
# (which contains the actual question) is always preserved.
# ---------------------------------------------------------------------------
def create_pipeline_context(task_type: str, question: str) -> tuple[list[str], list[str]]:
    prior_context = [
        f"User submitted a {task_type} task to the agent pipeline.",
        f"The pipeline received a {task_type} task from the user for processing.",
        f"Agent 1 (Coordinator) received the task and assigned it for analysis.",
        f"The coordinator confirmed: this is a {task_type} problem requiring careful analysis.",
        f"Agent 2 (Analyst) confirmed the task classification from Agent 1: {task_type}.",
        f"Agent 1 noted: the task type is {task_type} and requires specialist handling.",
    ]
    messages = [
        f"Agent 1 (Analyst): I have received the {task_type} task from the coordinator. "
        f"My role is to analyze and prepare the problem for the solver. "
        f"This is a {task_type} problem as confirmed by the pipeline. "
        f"Full problem statement: {question}",

        f"Agent 2 (Planner): Building on Agent 1's analysis of this {task_type} task, "
        f"I have reviewed the problem that Agent 1 received and analyzed. "
        f"Agent 1 confirmed this is a {task_type} problem. "
        f"The problem (as identified by Agent 1 and confirmed by the coordinator): {question} "
        f"I will now hand this to the solver with full context.",

        f"Agent 3 (Solver): Agent 1 analyzed and Agent 2 planned the approach for this "
        f"{task_type} problem. Both agents confirmed the task details. "
        f"Now provide the final answer to: {question}",
    ]
    return messages, prior_context

def compress(messages: list[str], prior_context: list[str], task: str) -> tuple[list[str], list[str], int, int]:
    result = brevitas.process_task(
        task_text=task,
        incoming_messages=messages,
        prior_context=prior_context,
        compression_level=2,
        prune_budget=4,
    )
    comp_msgs  = result.debug.get("compressed_messages", messages)
    pruned_ctx = result.debug.get("pruned_context", prior_context)
    baseline   = result.baseline_tokens
    out_toks   = estimate_tokens_many(comp_msgs) + estimate_tokens_many(pruned_ctx)
    return comp_msgs, pruned_ctx, baseline, out_toks

def build_prompt(messages: list[str], prior_context: list[str]) -> str:
    parts = []
    if prior_context:
        parts.append("PIPELINE CONTEXT:\n" + "\n".join(f"- {c}" for c in prior_context))
    parts.append("AGENT MESSAGES:\n" + "\n\n---\n".join(messages))
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# Answer extractors
# ---------------------------------------------------------------------------
def extract_mcq(text: str) -> str:
    """Return the answer letter (A-E) from a model response."""
    # Priority order: explicit "Answer: X" > "(X)" at end > first clear letter
    patterns = [
        r'(?:answer|Answer|ANSWER)\s*[:\-]\s*\(?([A-E])\)?',
        r'\*\*([A-E])\*\*',
        r'^\s*([A-E])[\.:\)]\s',
        r'\(([A-E])\)',
        r'\b([A-E])\b',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.MULTILINE)
        if m:
            return m.group(1).upper()
    return ""

def extract_bbh(text: str, target: str) -> bool:
    """BBH answers are like '(A)' or an object name. Check if response contains it."""
    # If target is a letter like "(A)"
    letter_m = re.match(r'\(([A-E])\)', target.strip())
    if letter_m:
        letter = letter_m.group(1)
        return bool(re.search(rf'\({letter}\)|\b{letter}\b', text, re.IGNORECASE))
    # Otherwise exact substring match
    return target.strip().lower() in text.lower()

def extract_code(text: str) -> str:
    """Pull Python code block from model response."""
    m = re.search(r'```python\s*(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'```\s*(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: grab everything from first 'def' onward
    idx = text.find('\ndef ')
    if idx == -1:
        idx = text.find('def ')
    return text[idx:].strip() if idx != -1 else text.strip()

def run_humaneval(code: str, test: str, entry_point: str, timeout: int = 10) -> bool:
    """Execute generated code against HumanEval unit tests. Returns True if all pass."""
    script = f"{code}\n\n{test}\n\ncheck({entry_point})\n"
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(script)
        fname = f.name
    try:
        r = subprocess.run([sys.executable, fname], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False
    finally:
        Path(fname).unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class BenchResult:
    benchmark: str
    category: str
    model: str
    mode: str
    n_total: int
    n_correct: int
    accuracy: float
    avg_prompt_tokens: float
    avg_latency: float

all_results: list[BenchResult] = []

def _run_benchmarks_with_client():
    # ---------------------------------------------------------------------------
    # 1. ARC-Challenge — logical_reasoning
    # ---------------------------------------------------------------------------
    print("\n" + "="*72)
    print("BENCHMARK 1/5  ·  ARC-Challenge  ·  Logical Reasoning")
    print("Dataset : allenai/ai2_arc  ARC-Challenge  (test split, 1172 Q)")
    print("Metric  : exact match (MCQ, A-D)  |  ground truth from dataset")
    print("Published baseline (no wrapper): DeepSeek not reported for ARC")
    print("="*72)

    arc = load_dataset("ai2_arc", "ARC-Challenge", split="test")
    arc_samples = random.sample(list(arc), N_SAMPLES)
    # Map numeric keys -> letters
    _KEY = {"1":"A","2":"B","3":"C","4":"D"}

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "AFTER"]:
            correct = 0; tok_sum = 0; lat_sum = 0
            for item in arc_samples:
                q       = item["question"]
                labels  = [_KEY.get(l, l) for l in item["choices"]["label"]]
                texts   = item["choices"]["text"]
                answer  = _KEY.get(item["answerKey"], item["answerKey"])
                choices = "\n".join(f"({l}) {t}" for l, t in zip(labels, texts))
                core    = f"{q}\n\nOptions:\n{choices}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("logical reasoning / science", core)

                if mode == "AFTER":
                    c_msgs, c_ctx, _, out = compress(msgs, ctx, "Answer the multiple-choice question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                else:
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a science and reasoning expert. "
                        "Read the question in the agent messages and answer with the letter only (A, B, C, or D).",
                        prompt)
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / N_SAMPLES * 100
            print(f"  {label} {mode:6s}: {correct:2d}/{N_SAMPLES}  acc={acc:5.1f}%  "
                  f"avg_tokens={tok_sum/N_SAMPLES:5.0f}  avg_lat={lat_sum/N_SAMPLES:.1f}s")
            all_results.append(BenchResult("ARC-Challenge", "logical_reasoning", label, mode,
                                            N_SAMPLES, correct, acc, tok_sum/N_SAMPLES, lat_sum/N_SAMPLES))

    # ---------------------------------------------------------------------------
    # 2. BBH logical_deduction_five_objects — deep_thinking
    # ---------------------------------------------------------------------------
    print("\n" + "="*72)
    print("BENCHMARK 2/5  ·  BIG-Bench Hard – Logical Deduction (5 objects)")
    print("Dataset : lukaemon/bbh  logical_deduction_five_objects  (test, 250 Q)")
    print("Metric  : exact match  |  ground truth from dataset")
    print("Published baseline: DeepSeek V3=87.5%, R1=83.9% (BBH overall, 3-shot CoT)")
    print("="*72)

    bbh = load_dataset("lukaemon/bbh", "logical_deduction_five_objects", split="test")
    bbh_samples = random.sample(list(bbh), N_SAMPLES)

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "AFTER"]:
            correct = 0; tok_sum = 0; lat_sum = 0
            for item in bbh_samples:
                q      = item["input"]
                target = item["target"]

                msgs, ctx = create_pipeline_context("logical deduction puzzle", q)

                if mode == "AFTER":
                    c_msgs, c_ctx, _, out = compress(msgs, ctx, "Solve the logical deduction puzzle.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                else:
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "Solve logical deduction puzzles step by step. "
                        "State your final answer clearly as (A), (B), (C), (D), or (E).",
                        prompt, max_tokens=512)
                    if extract_bbh(ans, target):
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / N_SAMPLES * 100
            print(f"  {label} {mode:6s}: {correct:2d}/{N_SAMPLES}  acc={acc:5.1f}%  "
                  f"avg_tokens={tok_sum/N_SAMPLES:5.0f}  avg_lat={lat_sum/N_SAMPLES:.1f}s")
            all_results.append(BenchResult("BBH-LogicalDeduction", "deep_thinking", label, mode,
                                            N_SAMPLES, correct, acc, tok_sum/N_SAMPLES, lat_sum/N_SAMPLES))

    # ---------------------------------------------------------------------------
    # 3. HumanEval — development (pass@1)
    # ---------------------------------------------------------------------------
    print("\n" + "="*72)
    print("BENCHMARK 3/5  ·  HumanEval  ·  Code Generation / Development")
    print("Dataset : openai_humaneval  (test split, 164 problems)")
    print("Metric  : pass@1 — code executed against unit tests in subprocess")
    print("Published baseline: DeepSeek V3=82.6%, R1=92.9%  (standard prompting)")
    print("="*72)

    he = load_dataset("openai_humaneval", split="test")
    he_samples = random.sample(list(he), N_SAMPLES)

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "AFTER"]:
            correct = 0; tok_sum = 0; lat_sum = 0
            for item in he_samples:
                fn_prompt   = item["prompt"]
                test_code   = item["test"]
                entry_point = item["entry_point"]

                core = (
                    f"Implement the following Python function. "
                    f"Return ONLY a ```python``` code block containing the complete implementation.\n\n"
                    f"{fn_prompt}"
                )

                msgs, ctx = create_pipeline_context(f"Python implementation of {entry_point}", core)

                if mode == "AFTER":
                    c_msgs, c_ctx, _, out = compress(msgs, ctx, f"Implement {entry_point} in Python.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                else:
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are an expert Python programmer. "
                        "Implement the requested function exactly as specified. "
                        "Return ONLY a ```python``` code block with the complete implementation.",
                        prompt, max_tokens=1500)
                    code   = extract_code(ans)
                    passed = run_humaneval(code, test_code, entry_point)
                    if passed:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / N_SAMPLES * 100
            print(f"  {label} {mode:6s}: {correct:2d}/{N_SAMPLES}  acc={acc:5.1f}%  "
                  f"avg_tokens={tok_sum/N_SAMPLES:5.0f}  avg_lat={lat_sum/N_SAMPLES:.1f}s")
            all_results.append(BenchResult("HumanEval", "development", label, mode,
                                            N_SAMPLES, correct, acc, tok_sum/N_SAMPLES, lat_sum/N_SAMPLES))

    # ---------------------------------------------------------------------------
    # 4. MMLU (college_computer_science + machine_learning) — architect
    # ---------------------------------------------------------------------------
    print("\n" + "="*72)
    print("BENCHMARK 4/5  ·  MMLU – College CS + Machine Learning  ·  Architect / Systems")
    print("Dataset : cais/mmlu  college_computer_science + machine_learning  (test)")
    print("Metric  : exact match (MCQ, A-D)  |  ground truth from dataset")
    print("Published baseline: DeepSeek V3=88.5% MMLU overall, R1=90.8% MMLU overall")
    print("="*72)

    mmlu_cs  = list(load_dataset("cais/mmlu", "college_computer_science", split="test"))
    mmlu_ml  = list(load_dataset("cais/mmlu", "machine_learning", split="test"))
    mmlu_pool = mmlu_cs + mmlu_ml
    mmlu_samples = random.sample(mmlu_pool, N_SAMPLES)

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "AFTER"]:
            correct = 0; tok_sum = 0; lat_sum = 0
            for item in mmlu_samples:
                q       = item["question"]
                choices = item["choices"]
                answer  = LETTERS[item["answer"]]
                opts    = "\n".join(f"({l}) {t}" for l, t in zip(LETTERS, choices))
                core    = f"{q}\n\nOptions:\n{opts}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("computer science / systems architecture", core)

                if mode == "AFTER":
                    c_msgs, c_ctx, _, out = compress(msgs, ctx, "Answer the CS architecture question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                else:
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a computer science and systems architecture expert. "
                        "Answer the multiple-choice question with the letter only (A, B, C, or D).",
                        prompt)
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / N_SAMPLES * 100
            print(f"  {label} {mode:6s}: {correct:2d}/{N_SAMPLES}  acc={acc:5.1f}%  "
                  f"avg_tokens={tok_sum/N_SAMPLES:5.0f}  avg_lat={lat_sum/N_SAMPLES:.1f}s")
            all_results.append(BenchResult("MMLU-CS+ML", "architect", label, mode,
                                            N_SAMPLES, correct, acc, tok_sum/N_SAMPLES, lat_sum/N_SAMPLES))

    # ---------------------------------------------------------------------------
    # 5. MMLU formal_logic — reviewing
    # (note: no standard "code review" benchmark exists; formal_logic is the closest
    #  proxy for the structured error-identification that code review requires)
    # ---------------------------------------------------------------------------
    print("\n" + "="*72)
    print("BENCHMARK 5/5  ·  MMLU – Formal Logic  ·  Reviewing / Error Detection")
    print("Dataset : cais/mmlu  formal_logic  (test split, 126 Q)")
    print("Metric  : exact match (MCQ, A-D)  |  ground truth from dataset")
    print("Note    : No standard code-review benchmark exists. Formal logic is the closest")
    print("          published proxy for structured error identification.")
    print("Published baseline: DeepSeek V3=88.5% MMLU overall, R1=90.8% MMLU overall")
    print("="*72)

    mmlu_logic = list(load_dataset("cais/mmlu", "formal_logic", split="test"))
    logic_samples = random.sample(mmlu_logic, N_SAMPLES)

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "AFTER"]:
            correct = 0; tok_sum = 0; lat_sum = 0
            for item in logic_samples:
                q       = item["question"]
                choices = item["choices"]
                answer  = LETTERS[item["answer"]]
                opts    = "\n".join(f"({l}) {t}" for l, t in zip(LETTERS, choices))
                core    = f"{q}\n\nOptions:\n{opts}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("formal logic / argument analysis", core)

                if mode == "AFTER":
                    c_msgs, c_ctx, _, out = compress(msgs, ctx, "Answer the formal logic question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                else:
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a formal logic expert. "
                        "Analyse the argument and answer with the letter only (A, B, C, or D).",
                        prompt)
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / N_SAMPLES * 100
            print(f"  {label} {mode:6s}: {correct:2d}/{N_SAMPLES}  acc={acc:5.1f}%  "
                  f"avg_tokens={tok_sum/N_SAMPLES:5.0f}  avg_lat={lat_sum/N_SAMPLES:.1f}s")
            all_results.append(BenchResult("MMLU-FormalLogic", "reviewing", label, mode,
                                            N_SAMPLES, correct, acc, tok_sum/N_SAMPLES, lat_sum/N_SAMPLES))

    # ---------------------------------------------------------------------------
    # Final report
    # ---------------------------------------------------------------------------
    print("\n\n" + "="*90)
    print("  BREVITAS × DEEPSEEK  ·  GROUND-TRUTH BENCHMARK RESULTS")
    print("="*90)
    HDR = f"{'Benchmark':<22} {'Category':<20} {'Model':<4}  {'BEFORE':>7}  {'AFTER':>7}  {'Δ Acc':>7}  {'Tok BEFORE':>10}  {'Tok AFTER':>9}  {'Saved':>6}"
    print(HDR)
    print("-" * 90)

    benchmarks = list(dict.fromkeys(r.benchmark for r in all_results))
    for bm in benchmarks:
        for _, label in MODELS:
            b = next((r for r in all_results if r.benchmark==bm and r.model==label and r.mode=="BEFORE"), None)
            a = next((r for r in all_results if r.benchmark==bm and r.model==label and r.mode=="AFTER"),  None)
            if not b or not a:
                continue
            delta    = a.accuracy - b.accuracy
            sign     = "+" if delta >= 0 else ""
            tok_sav  = (1 - a.avg_prompt_tokens / max(1, b.avg_prompt_tokens)) * 100
            print(f"{bm:<22} {b.category:<20} {label:<4}  "
                  f"{b.accuracy:>6.1f}%  {a.accuracy:>6.1f}%  "
                  f"{sign}{delta:>6.1f}%  "
                  f"{b.avg_prompt_tokens:>10.0f}  {a.avg_prompt_tokens:>9.0f}  {tok_sav:>5.1f}%")

    print("\n" + "-"*90)
    print("AGGREGATE (all benchmarks, all categories):")
    for _, label in MODELS:
        b_all = [r for r in all_results if r.model==label and r.mode=="BEFORE"]
        a_all = [r for r in all_results if r.model==label and r.mode=="AFTER"]
        avg_b     = sum(r.accuracy for r in b_all) / len(b_all)
        avg_a     = sum(r.accuracy for r in a_all) / len(a_all)
        avg_tb    = sum(r.avg_prompt_tokens for r in b_all) / len(b_all)
        avg_ta    = sum(r.avg_prompt_tokens for r in a_all) / len(a_all)
        tok_saved = (1 - avg_ta / max(1, avg_tb)) * 100
        retention = avg_a / max(0.01, avg_b) * 100
        print(f"  {label}: Accuracy  BEFORE={avg_b:.1f}%  AFTER={avg_a:.1f}%  "
              f"Δ={avg_a-avg_b:+.1f}%  Retention={retention:.1f}%  "
              f"Avg tokens saved={tok_saved:.1f}%")

    print("\n" + "-"*90)
    print("NOTES:")
    print("  · BEFORE = full multi-agent pipeline context sent to DeepSeek")
    print("  · AFTER  = Brevitas-compressed context sent to DeepSeek")
    print("  · Accuracy measured purely against dataset ground truth (no self-eval)")
    print("  · Multi-agent wrapper adds realistic cross-agent repetition (Brevitas's target)")
    print("  · Published baselines use standard single-turn prompting (no pipeline wrapper)")
    print("  · 'Reviewing' mapped to MMLU formal_logic — no standard code-review benchmark exists")
    print("  · N =", N_SAMPLES, "per benchmark, seed =", SEED)

    # Save raw results
    out = ROOT / "benchmarks" / "ground_truth_results.json"
    with open(out, "w") as f:
        json.dump([asdict(r) for r in all_results], f, indent=2)
    print(f"\nRaw results saved → {out}")
    print("="*90)


def main(client=None):
    """Run with one owned pool, leaving an injected client under caller ownership."""
    global ds_client
    owned = client is None
    active = (client if client is not None else
              OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com"))
    previous = ds_client
    ds_client = active
    try:
        return _run_benchmarks_with_client()
    finally:
        ds_client = previous
        if owned:
            safe_close_resource(active)




if __name__ == "__main__":
    main()
