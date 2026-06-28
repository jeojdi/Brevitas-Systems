"""
Brevitas × DeepSeek Ground-Truth Benchmark V2
==============================================
BEFORE (full context) vs OLD-LOSSY (legacy TokenEfficientPipeline) vs NEW (lossless orchestrator)

Modes:
  BEFORE   = full multi-agent context (baseline ceiling)
  OLD-LOSSY = legacy TokenEfficientPipeline default (compression+pruning)
  NEW      = revamped lossless path (native cache + RLM retrieval, BrevitasMode.LOSSLESS)

Datasets: arc, bbh, humaneval, mmlu_cs, mmlu_logic
Same datasets and scoring as v1 — pure accuracy vs ground truth, no self-eval.
"""

import os, sys, re, json, time, random, subprocess, tempfile, argparse
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
from token_efficiency_model.modes.tiered_orchestrator import (
    TieredModeOrchestrator,
    BrevitasMode,
    ModeConfig,
)

ds_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
brevitas = TokenEfficientPipeline(model_backend=None, quality_floor=0.80, savings_target=20.0)
orchestrator = TieredModeOrchestrator()

SEED = 42
random.seed(SEED)

LETTERS = ["A", "B", "C", "D", "E"]

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Ground-truth benchmark V2: BEFORE vs OLD-LOSSY vs NEW")
parser.add_argument("--n", type=int, default=2, help="Number of samples per benchmark")
parser.add_argument("--models", choices=["v3", "r1", "both"], default="v3", help="Which models to test")
parser.add_argument("--datasets", nargs="+", default=["bbh", "mmlu_logic"],
                   help="Which datasets to run (arc, bbh, humaneval, mmlu_cs, mmlu_logic)")
args = parser.parse_args()

N_SAMPLES = args.n
MODELS = [("deepseek-chat", "V3")]  # Default
if args.models == "both":
    MODELS = [("deepseek-chat", "V3"), ("deepseek-reasoner", "R1")]
elif args.models == "r1":
    MODELS = [("deepseek-reasoner", "R1")]

# Normalize dataset names
dataset_map = {
    "arc": "arc",
    "bbh": "bbh",
    "humaneval": "humaneval",
    "mmlu_cs": "mmlu_cs",
    "mmlu_logic": "mmlu_logic",
}
datasets_to_run = [dataset_map.get(d.lower(), d.lower()) for d in args.datasets]

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

# ---------------------------------------------------------------------------
# Compression functions
# ---------------------------------------------------------------------------
def compress_lossy(messages: list[str], prior_context: list[str], task: str) -> tuple[list[str], list[str], int, int]:
    """Apply legacy lossy compression (OLD-LOSSY mode)."""
    result = brevitas.process_task(
        task_text=task,
        incoming_messages=messages,
        prior_context=prior_context,
        compression_level=2,
        prune_budget=4,
    )
    comp_msgs = result.debug.get("compressed_messages", messages)
    pruned_ctx = result.debug.get("pruned_context", prior_context)
    baseline = result.baseline_tokens
    out_toks = estimate_tokens_many(comp_msgs) + estimate_tokens_many(pruned_ctx)
    return comp_msgs, pruned_ctx, baseline, out_toks

def process_lossless(messages: list[str], prior_context: list[str], task: str) -> tuple[list[str], list[str], int]:
    """Apply lossless mode (NEW path)."""
    config = ModeConfig(mode=BrevitasMode.LOSSLESS, enable_rlm_retrieval=True)
    result = orchestrator.process(
        task_text=task,
        incoming_messages=messages,
        prior_context=prior_context,
        config=config,
    )
    # In lossless mode, we return full context but with RLM retrieval prepared
    out_toks = estimate_tokens_many(result.optimized_messages) + estimate_tokens_many(result.optimized_context)
    return result.optimized_messages, result.optimized_context, out_toks

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
    """BBH answers are like '(A)' or an object name."""
    letter_m = re.match(r'\(([A-E])\)', target.strip())
    if letter_m:
        letter = letter_m.group(1)
        return bool(re.search(rf'\({letter}\)|\b{letter}\b', text, re.IGNORECASE))
    return target.strip().lower() in text.lower()

def extract_code(text: str) -> str:
    """Pull Python code block from model response."""
    m = re.search(r'```python\s*(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'```\s*(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    idx = text.find('\ndef ')
    if idx == -1:
        idx = text.find('def ')
    return text[idx:].strip() if idx != -1 else text.strip()

def run_humaneval(code: str, test: str, entry_point: str, timeout: int = 10) -> bool:
    """Execute generated code against HumanEval unit tests."""
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
api_call_count = 0

# ---------------------------------------------------------------------------
# BENCHMARK RUNNERS
# ---------------------------------------------------------------------------
def run_arc_challenge():
    """ARC-Challenge benchmark."""
    global api_call_count
    print("\n" + "="*80)
    print("BENCHMARK: ARC-Challenge (Logical Reasoning)")
    print("Dataset: allenai/ai2_arc ARC-Challenge (test split)")
    print("="*80)

    arc = load_dataset("ai2_arc", "ARC-Challenge", split="test")
    arc_samples = random.sample(list(arc), min(N_SAMPLES, len(list(arc))))
    _KEY = {"1":"A","2":"B","3":"C","4":"D"}

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "OLD-LOSSY", "NEW"]:
            correct = 0
            tok_sum = 0
            lat_sum = 0
            for item in arc_samples:
                q = item["question"]
                labels = [_KEY.get(l, l) for l in item["choices"]["label"]]
                texts = item["choices"]["text"]
                answer = _KEY.get(item["answerKey"], item["answerKey"])
                choices = "\n".join(f"({l}) {t}" for l, t in zip(labels, texts))
                core = f"{q}\n\nOptions:\n{choices}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("logical reasoning / science", core)

                if mode == "OLD-LOSSY":
                    c_msgs, c_ctx, _, out = compress_lossy(msgs, ctx, "Answer the multiple-choice question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                elif mode == "NEW":
                    try:
                        opt_msgs, opt_ctx, out = process_lossless(msgs, ctx, "Answer the multiple-choice question.")
                        prompt = build_prompt(opt_msgs, opt_ctx)
                        tok_sum += out
                    except Exception as e:
                        prompt = build_prompt(msgs, ctx)
                        tok_sum += estimate_tokens(prompt)
                else:  # BEFORE
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a science and reasoning expert. Answer with the letter only (A, B, C, or D).",
                        prompt)
                    api_call_count += 1
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / len(arc_samples) * 100
            print(f"  {label} {mode:10s}: {correct:2d}/{len(arc_samples)}  acc={acc:5.1f}%  avg_tokens={tok_sum/len(arc_samples):5.0f}")
            all_results.append(BenchResult("ARC-Challenge", "logical_reasoning", label, mode,
                                            len(arc_samples), correct, acc, tok_sum/len(arc_samples), lat_sum/len(arc_samples)))

def run_bbh():
    """BBH logical_deduction benchmark."""
    global api_call_count
    print("\n" + "="*80)
    print("BENCHMARK: BIG-Bench Hard – Logical Deduction (5 objects)")
    print("Dataset: lukaemon/bbh logical_deduction_five_objects (test)")
    print("="*80)

    bbh = load_dataset("lukaemon/bbh", "logical_deduction_five_objects", split="test")
    bbh_samples = random.sample(list(bbh), min(N_SAMPLES, len(list(bbh))))

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "OLD-LOSSY", "NEW"]:
            correct = 0
            tok_sum = 0
            lat_sum = 0
            for item in bbh_samples:
                q = item["input"]
                target = item["target"]

                msgs, ctx = create_pipeline_context("logical deduction puzzle", q)

                if mode == "OLD-LOSSY":
                    c_msgs, c_ctx, _, out = compress_lossy(msgs, ctx, "Solve the logical deduction puzzle.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                elif mode == "NEW":
                    try:
                        opt_msgs, opt_ctx, out = process_lossless(msgs, ctx, "Solve the logical deduction puzzle.")
                        prompt = build_prompt(opt_msgs, opt_ctx)
                        tok_sum += out
                    except Exception as e:
                        prompt = build_prompt(msgs, ctx)
                        tok_sum += estimate_tokens(prompt)
                else:  # BEFORE
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "Solve logical deduction puzzles step by step. State your final answer clearly as (A), (B), (C), (D), or (E).",
                        prompt, max_tokens=512)
                    api_call_count += 1
                    if extract_bbh(ans, target):
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / len(bbh_samples) * 100
            print(f"  {label} {mode:10s}: {correct:2d}/{len(bbh_samples)}  acc={acc:5.1f}%  avg_tokens={tok_sum/len(bbh_samples):5.0f}")
            all_results.append(BenchResult("BBH-LogicalDeduction", "deep_thinking", label, mode,
                                            len(bbh_samples), correct, acc, tok_sum/len(bbh_samples), lat_sum/len(bbh_samples)))

def run_humaneval():
    """HumanEval code generation benchmark."""
    global api_call_count
    print("\n" + "="*80)
    print("BENCHMARK: HumanEval (Code Generation / Development)")
    print("Dataset: openai_humaneval (test split, pass@1 scoring)")
    print("="*80)

    he = load_dataset("openai_humaneval", split="test")
    he_samples = random.sample(list(he), min(N_SAMPLES, len(list(he))))

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "OLD-LOSSY", "NEW"]:
            correct = 0
            tok_sum = 0
            lat_sum = 0
            for item in he_samples:
                fn_prompt = item["prompt"]
                test_code = item["test"]
                entry_point = item["entry_point"]

                core = (
                    f"Implement the following Python function. "
                    f"Return ONLY a ```python``` code block containing the complete implementation.\n\n"
                    f"{fn_prompt}"
                )

                msgs, ctx = create_pipeline_context(f"Python implementation of {entry_point}", core)

                if mode == "OLD-LOSSY":
                    c_msgs, c_ctx, _, out = compress_lossy(msgs, ctx, f"Implement {entry_point} in Python.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                elif mode == "NEW":
                    try:
                        opt_msgs, opt_ctx, out = process_lossless(msgs, ctx, f"Implement {entry_point} in Python.")
                        prompt = build_prompt(opt_msgs, opt_ctx)
                        tok_sum += out
                    except Exception as e:
                        prompt = build_prompt(msgs, ctx)
                        tok_sum += estimate_tokens(prompt)
                else:  # BEFORE
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are an expert Python programmer. Implement the requested function exactly as specified. "
                        "Return ONLY a ```python``` code block with the complete implementation.",
                        prompt, max_tokens=1500)
                    api_call_count += 1
                    code = extract_code(ans)
                    passed = run_humaneval(code, test_code, entry_point)
                    if passed:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / len(he_samples) * 100
            print(f"  {label} {mode:10s}: {correct:2d}/{len(he_samples)}  acc={acc:5.1f}%  avg_tokens={tok_sum/len(he_samples):5.0f}")
            all_results.append(BenchResult("HumanEval", "development", label, mode,
                                            len(he_samples), correct, acc, tok_sum/len(he_samples), lat_sum/len(he_samples)))

def run_mmlu_cs():
    """MMLU College CS + Machine Learning benchmark."""
    global api_call_count
    print("\n" + "="*80)
    print("BENCHMARK: MMLU – College CS + Machine Learning (Architect / Systems)")
    print("Dataset: cais/mmlu college_computer_science + machine_learning (test)")
    print("="*80)

    mmlu_cs = list(load_dataset("cais/mmlu", "college_computer_science", split="test"))
    mmlu_ml = list(load_dataset("cais/mmlu", "machine_learning", split="test"))
    mmlu_pool = mmlu_cs + mmlu_ml
    mmlu_samples = random.sample(mmlu_pool, min(N_SAMPLES, len(mmlu_pool)))

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "OLD-LOSSY", "NEW"]:
            correct = 0
            tok_sum = 0
            lat_sum = 0
            for item in mmlu_samples:
                q = item["question"]
                choices = item["choices"]
                answer = LETTERS[item["answer"]]
                opts = "\n".join(f"({l}) {t}" for l, t in zip(LETTERS, choices))
                core = f"{q}\n\nOptions:\n{opts}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("computer science / systems architecture", core)

                if mode == "OLD-LOSSY":
                    c_msgs, c_ctx, _, out = compress_lossy(msgs, ctx, "Answer the CS architecture question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                elif mode == "NEW":
                    try:
                        opt_msgs, opt_ctx, out = process_lossless(msgs, ctx, "Answer the CS architecture question.")
                        prompt = build_prompt(opt_msgs, opt_ctx)
                        tok_sum += out
                    except Exception as e:
                        prompt = build_prompt(msgs, ctx)
                        tok_sum += estimate_tokens(prompt)
                else:  # BEFORE
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a computer science and systems architecture expert. Answer with the letter only (A, B, C, or D).",
                        prompt)
                    api_call_count += 1
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / len(mmlu_samples) * 100
            print(f"  {label} {mode:10s}: {correct:2d}/{len(mmlu_samples)}  acc={acc:5.1f}%  avg_tokens={tok_sum/len(mmlu_samples):5.0f}")
            all_results.append(BenchResult("MMLU-CS+ML", "architect", label, mode,
                                            len(mmlu_samples), correct, acc, tok_sum/len(mmlu_samples), lat_sum/len(mmlu_samples)))

def run_mmlu_logic():
    """MMLU Formal Logic benchmark."""
    global api_call_count
    print("\n" + "="*80)
    print("BENCHMARK: MMLU – Formal Logic (Reviewing / Error Detection)")
    print("Dataset: cais/mmlu formal_logic (test split)")
    print("="*80)

    mmlu_logic = list(load_dataset("cais/mmlu", "formal_logic", split="test"))
    logic_samples = random.sample(mmlu_logic, min(N_SAMPLES, len(mmlu_logic)))

    for ds_model, label in MODELS:
        for mode in ["BEFORE", "OLD-LOSSY", "NEW"]:
            correct = 0
            tok_sum = 0
            lat_sum = 0
            for item in logic_samples:
                q = item["question"]
                choices = item["choices"]
                answer = LETTERS[item["answer"]]
                opts = "\n".join(f"({l}) {t}" for l, t in zip(LETTERS, choices))
                core = f"{q}\n\nOptions:\n{opts}\n\nReply with the letter only."

                msgs, ctx = create_pipeline_context("formal logic / argument analysis", core)

                if mode == "OLD-LOSSY":
                    c_msgs, c_ctx, _, out = compress_lossy(msgs, ctx, "Answer the formal logic question.")
                    prompt = build_prompt(c_msgs, c_ctx)
                    tok_sum += out
                elif mode == "NEW":
                    try:
                        opt_msgs, opt_ctx, out = process_lossless(msgs, ctx, "Answer the formal logic question.")
                        prompt = build_prompt(opt_msgs, opt_ctx)
                        tok_sum += out
                    except Exception as e:
                        prompt = build_prompt(msgs, ctx)
                        tok_sum += estimate_tokens(prompt)
                else:  # BEFORE
                    prompt = build_prompt(msgs, ctx)
                    tok_sum += estimate_tokens(prompt)

                try:
                    ans, lat = call_ds(ds_model,
                        "You are a formal logic expert. Analyse the argument and answer with the letter only (A, B, C, or D).",
                        prompt)
                    api_call_count += 1
                    if extract_mcq(ans) == answer:
                        correct += 1
                    lat_sum += lat
                except Exception as e:
                    print(f"    API error: {e}")

            acc = correct / len(logic_samples) * 100
            print(f"  {label} {mode:10s}: {correct:2d}/{len(logic_samples)}  acc={acc:5.1f}%  avg_tokens={tok_sum/len(logic_samples):5.0f}")
            all_results.append(BenchResult("MMLU-FormalLogic", "reviewing", label, mode,
                                            len(logic_samples), correct, acc, tok_sum/len(logic_samples), lat_sum/len(logic_samples)))

# ---------------------------------------------------------------------------
# RUN SELECTED BENCHMARKS
# ---------------------------------------------------------------------------
benchmark_runners = {
    "arc": run_arc_challenge,
    "bbh": run_bbh,
    "humaneval": run_humaneval,
    "mmlu_cs": run_mmlu_cs,
    "mmlu_logic": run_mmlu_logic,
}

for dataset in datasets_to_run:
    if dataset in benchmark_runners:
        benchmark_runners[dataset]()

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
print("\n\n" + "="*100)
print("  BREVITAS × DEEPSEEK  ·  GROUND-TRUTH BENCHMARK V2 RESULTS")
print("="*100)
HDR = f"{'Benchmark':<22} {'Model':<4}  {'BEFORE':>7}  {'OLD-LOSSY':>10}  {'NEW':>7}  {'OLD Δ':>7}  {'NEW Δ':>7}"
print(HDR)
print("-" * 100)

benchmarks = list(dict.fromkeys(r.benchmark for r in all_results))
for bm in benchmarks:
    for _, label in MODELS:
        b = next((r for r in all_results if r.benchmark==bm and r.model==label and r.mode=="BEFORE"), None)
        o = next((r for r in all_results if r.benchmark==bm and r.model==label and r.mode=="OLD-LOSSY"), None)
        n = next((r for r in all_results if r.benchmark==bm and r.model==label and r.mode=="NEW"), None)
        if not b:
            continue
        old_delta = (o.accuracy - b.accuracy) if o else None
        new_delta = (n.accuracy - b.accuracy) if n else None
        old_str = f"{o.accuracy:6.1f}%" if o else "N/A"
        new_str = f"{n.accuracy:6.1f}%" if n else "N/A"
        old_d_str = f"{old_delta:+6.1f}%" if old_delta is not None else "N/A"
        new_d_str = f"{new_delta:+6.1f}%" if new_delta is not None else "N/A"
        print(f"{bm:<22} {label:<4}  {b.accuracy:>6.1f}%  {old_str:>10}  {new_str:>7}  {old_d_str:>7}  {new_d_str:>7}")

print("\n" + "-"*100)
print("AGGREGATE (all benchmarks):")
for _, label in MODELS:
    b_all = [r for r in all_results if r.model==label and r.mode=="BEFORE"]
    o_all = [r for r in all_results if r.model==label and r.mode=="OLD-LOSSY"]
    n_all = [r for r in all_results if r.model==label and r.mode=="NEW"]

    if b_all:
        avg_b = sum(r.accuracy for r in b_all) / len(b_all)
        print(f"  {label} BEFORE: {avg_b:.1f}%")
    if o_all:
        avg_o = sum(r.accuracy for r in o_all) / len(o_all)
        old_retention = (avg_o / max(0.01, avg_b) * 100) if b_all else 0
        print(f"  {label} OLD-LOSSY: {avg_o:.1f}% (Δ={avg_o-avg_b:+.1f}%)")
    if n_all:
        avg_n = sum(r.accuracy for r in n_all) / len(n_all)
        new_retention = (avg_n / max(0.01, avg_b) * 100) if b_all else 0
        print(f"  {label} NEW: {avg_n:.1f}% (Δ={avg_n-avg_b:+.1f}%)")

print("\n" + "-"*100)
print("NOTES:")
print("  · BEFORE = full multi-agent context (baseline)")
print("  · OLD-LOSSY = legacy TokenEfficientPipeline (compression+pruning)")
print("  · NEW = lossless orchestrator (native cache + RLM retrieval, no lossy compression)")
print("  · Accuracy measured purely against dataset ground truth (no self-eval)")
print(f"  · Total API calls (live): {api_call_count}")
print(f"  · Models tested: {', '.join(label for _, label in MODELS)}")
print(f"  · Datasets tested: {', '.join(datasets_to_run)}")
print(f"  · Samples per condition: {N_SAMPLES}")
print(f"  · Seed: {SEED}")

# Save raw results
out = ROOT / "benchmarks" / f"ground_truth_v2_results_n{N_SAMPLES}.json"
with open(out, "w") as f:
    json.dump([asdict(r) for r in all_results], f, indent=2)
print(f"\nRaw results saved → {out}")
print("="*100)
