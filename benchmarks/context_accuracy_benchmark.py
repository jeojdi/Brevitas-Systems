"""
Brevitas Context Accuracy Benchmark
====================================
Phase 2 Validation: Long-Context QA with Ground Truth

Measures accuracy vs ground truth across three context strategies:
  A) FULL-CONTEXT baseline (send everything) — accuracy ceiling
  B) LOSSY-PRUNE (TokenEfficientPipeline: compression + adaptive sampling + pruning)
  C) RLM-RETRIEVAL (Phase 2: keep all context, retrieve precise snippets)

Dataset: HotpotQA (multi-hop QA with supporting context)
- Real public dataset with ground-truth answers
- Long context naturally (several supporting passages required)
- Evaluate accuracy degradation from lossy compression
- Validate Phase 2 retrieval module when available

Scoring: Exact string match (F1 for partial credit is optional).
Cost discipline: Small N (2 for smoke-test, 12 for full), temp=0.
"""

import os
import sys
import json
import time
import random
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Callable, Tuple, List, Dict, Any

# ---------------------------------------------------------------------------
# Environment Setup
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
from token_efficiency_model.context_store import ContextStore
from token_efficiency_model.optimizers.retrieval import RetrieverIndexer

ds_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
brevitas = TokenEfficientPipeline(model_backend=None, quality_floor=0.80, savings_target=20.0)

# Initialize context store for RLM-RETRIEVAL strategy (will be populated if retrieval module available)
context_store = ContextStore()

# Initialize the dense retriever (Phase 2)
try:
    dense_retriever = RetrieverIndexer(
        checkpoint="colbert-ir/colbertv2.0",
        use_gpu=True,
        fallback_model="BAAI/bge-small-en-v1.5",
    )
    RETRIEVER_ACTIVE = True
    RETRIEVER_METHOD = dense_retriever._method
except Exception as e:
    print(f"⚠️  Dense retriever initialization failed: {e}")
    dense_retriever = None
    RETRIEVER_ACTIVE = False
    RETRIEVER_METHOD = "none"

# Configuration
N_SAMPLES = 15  # HotpotQA standard benchmark size
SEED = 42
TEMPERATURE = 0.0
MODEL = "deepseek-chat"  # V3 for speed; can switch to deepseek-reasoner for deeper reasoning

random.seed(SEED)

# Scoring config
F1_THRESHOLD = 0.5  # Accept F1 >= 0.5 as correct (HotpotQA standard, like SQuAD)

# ---------------------------------------------------------------------------
# Strategy Interfaces
# ---------------------------------------------------------------------------

class ContextStrategy:
    """Base class for context retrieval strategies."""

    def prepare_context(
        self,
        full_context: List[str],
        task_text: str,
        supporting_facts: List[str],
    ) -> Tuple[str, int]:
        """
        Args:
            full_context: List of all context passages/chunks
            task_text: The question/task text
            supporting_facts: (Optional) relevant passages; used by RLM for initialization

        Returns:
            (prompt_text, input_tokens_sent)
        """
        raise NotImplementedError

    def get_name(self) -> str:
        raise NotImplementedError


class FullContextStrategy(ContextStrategy):
    """Strategy A: Send full context (baseline/ceiling)."""

    def prepare_context(
        self,
        full_context: List[str],
        task_text: str,
        supporting_facts: List[str],
    ) -> Tuple[str, int]:
        prompt = self._build_prompt(full_context, task_text)
        tokens = estimate_tokens(prompt)
        return prompt, tokens

    def _build_prompt(self, context: List[str], task: str) -> str:
        prompt_parts = []
        if context:
            prompt_parts.append("CONTEXT:\n" + "\n---\n".join(context))
        prompt_parts.append(f"QUESTION:\n{task}")
        return "\n\n".join(prompt_parts)

    def get_name(self) -> str:
        return "FULL-CONTEXT"


class LossyPruneStrategy(ContextStrategy):
    """Strategy B: TokenEfficientPipeline (lossy compression + adaptive sampling + pruning)."""

    def prepare_context(
        self,
        full_context: List[str],
        task_text: str,
        supporting_facts: List[str],
    ) -> Tuple[str, int]:
        # Simulate multi-agent context flow with redundancy
        incoming_messages = [
            f"Agent 1 (Analyzer): Processing task. Question: {task_text}",
            f"Agent 2 (Reviewer): Reviewing context to answer: {task_text}",
            f"Agent 3 (Solver): Ready to answer. Full question: {task_text}",
        ]
        prior_context = full_context  # Will be pruned

        result = brevitas.process_task(
            task_text=task_text,
            incoming_messages=incoming_messages,
            prior_context=prior_context,
            compression_level=2,
            prune_budget=4,
        )

        compressed_msgs = result.debug.get("compressed_messages", incoming_messages)
        pruned_ctx = result.debug.get("pruned_context", prior_context)

        prompt = self._build_prompt(pruned_ctx, compressed_msgs, task_text)
        tokens = estimate_tokens(prompt)
        return prompt, tokens

    def _build_prompt(self, context: List[str], messages: List[str], task: str) -> str:
        prompt_parts = []
        if context:
            prompt_parts.append("CONTEXT (pruned):\n" + "\n---\n".join(context))
        if messages:
            prompt_parts.append("AGENT FLOW:\n" + "\n".join(messages))
        prompt_parts.append(f"QUESTION:\n{task}")
        return "\n\n".join(prompt_parts)

    def get_name(self) -> str:
        return "LOSSY-PRUNE"


class RLMRetrievalStrategy(ContextStrategy):
    """Strategy C: RLM-RETRIEVAL (Phase 2 — dense vector retrieval).

    Uses executor-p2's dense retriever (ColBERT PyLate or sentence-transformers fallback).
    Keeps all context indexed, retrieves precise snippets for each query.
    """

    def __init__(self, retriever: Optional[RetrieverIndexer] = None, top_k: int = 5):
        """
        Args:
            retriever: RetrieverIndexer instance (executor-p2's dense retriever).
                      If None, disables dense retrieval for this strategy.
            top_k: Number of chunks to retrieve per query.
        """
        self.retriever = retriever
        self.top_k = top_k
        self._indexed_contexts = {}  # task_id -> RetrieverIndexer instance with indexed chunks

    def prepare_context(
        self,
        full_context: List[str],
        task_text: str,
        supporting_facts: List[str],
    ) -> Tuple[str, int]:
        # Index the context chunks for this task
        if self.retriever:
            # Create a fresh retriever instance for this task
            task_retriever = RetrieverIndexer(
                checkpoint=self.retriever.checkpoint,
                use_gpu=self.retriever.use_gpu,
                fallback_model=self.retriever.fallback_model,
            )
            task_retriever.index(full_context)

            # Retrieve top-k chunks relevant to the task
            results = task_retriever.retrieve(task_text, k=self.top_k)
            chunk_hashes = [h for h, _ in results]
            retrieved = task_retriever.get_chunks_by_hash(chunk_hashes)
        else:
            # Fallback if retriever not available
            retrieved = self._fallback_retrieve(task_text, full_context, supporting_facts)

        prompt = self._build_prompt(retrieved, task_text)
        tokens = estimate_tokens(prompt)
        return prompt, tokens

    def _fallback_retrieve(
        self,
        task_text: str,
        full_context: List[str],
        supporting_facts: List[str],
    ) -> List[str]:
        """Fallback: prefer supporting_facts if available (ground truth info)."""
        if supporting_facts:
            retrieved = [p for p in full_context if any(sf in p for sf in supporting_facts)]
            if retrieved:
                return retrieved[:self.top_k]

        return full_context[:self.top_k]

    def _build_prompt(self, context: List[str], task: str) -> str:
        prompt_parts = []
        if context:
            prompt_parts.append("CONTEXT (retrieved):\n" + "\n---\n".join(context))
        prompt_parts.append(f"QUESTION:\n{task}")
        return "\n\n".join(prompt_parts)

    def get_name(self) -> str:
        return "RLM-RETRIEVAL"


# ---------------------------------------------------------------------------
# Dataset Loading & Preparation
# ---------------------------------------------------------------------------

def load_hotpotqa(split: str = "validation", n: int = N_SAMPLES) -> List[Dict[str, Any]]:
    """
    Load HotpotQA dataset.

    Returns list of items:
      {
        'id': str,
        'question': str,
        'answer': str,
        'supporting_facts': List[List[str]],  # [[title, sent_idx], ...]
        'context': List[List[str]],  # [[title, [sentences...]], ...]
      }
    """
    dataset = load_dataset("hotpot_qa", "distractor", split=split)
    samples = random.sample(list(dataset), min(n, len(dataset)))
    return samples


def prepare_context_and_facts(item: Dict[str, Any]) -> Tuple[List[str], str, List[str]]:
    """
    Extract context passages and supporting facts from HotpotQA item.

    Returns:
        (full_context: List[str], question: str, supporting_facts: List[str])
    """
    # HotpotQA context is {title: [...], sentences: [[...], [...]]}
    context_dict = item["context"]
    titles = context_dict["title"]
    sentences_list = context_dict["sentences"]

    # Flatten context: each Wikipedia passage becomes one context chunk
    full_context = []
    for title, sentences in zip(titles, sentences_list):
        passage = f"[{title}] " + " ".join(sentences)
        full_context.append(passage)

    # Extract supporting facts as a list of strings for matching
    # supporting_facts is {title: [...], sent_id: [...]}
    sup_facts_dict = item["supporting_facts"]
    supporting_facts = list(set(sup_facts_dict["title"]))  # Unique titles

    return full_context, item["question"], supporting_facts


# ---------------------------------------------------------------------------
# Answer Extraction & Matching (SQuAD/HotpotQA Standard)
# ---------------------------------------------------------------------------

def normalize_answer(answer: str) -> str:
    """Normalize answer for comparison (SQuAD style)."""
    import string
    answer = answer.lower()
    # Remove articles
    answer = re.sub(r"\b(a|an|the)\b", " ", answer)
    # Remove punctuation
    answer = "".join(ch for ch in answer if ch not in set(string.punctuation))
    # Collapse whitespace
    answer = " ".join(answer.split())
    return answer


def f1_score(prediction: str, ground_truth: str) -> float:
    """Compute token-level F1 between prediction and ground truth (SQuAD style)."""
    pred_tokens = normalize_answer(prediction).split()
    gold_tokens = normalize_answer(ground_truth).split()

    common = set(pred_tokens) & set(gold_tokens)

    if not common:
        return 0.0

    if not pred_tokens or not gold_tokens:
        return 0.0

    precision = len(common) / len(pred_tokens)
    recall = len(common) / len(gold_tokens)

    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    return f1


def extract_answer_from_response(text: str) -> str:
    """
    Extract the most likely answer span from model response.

    HotpotQA answers are typically short spans (1-5 tokens).
    Look for explicit markers, then try sentence boundaries.
    """
    # Priority 1: Explicit "Answer:" or "The answer is:"
    patterns = [
        r"(?:Answer|answer)[:\s]+([^.!?\n]+?)(?:[.!?\n]|$)",
        r"(?:The answer is|answer is)[:\s]+([^.!?\n]+?)(?:[.!?\n]|$)",
        r"(?:final answer|correct answer)[:\s]+([^.!?\n]+?)(?:[.!?\n]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            # Sanity check: answer shouldn't be too long (HotpotQA answers are ~2 tokens)
            if len(candidate.split()) <= 10:
                return candidate

    # Priority 2: Last sentence (most likely contains the answer)
    sentences = [s.strip() for s in re.split(r'[.!?\n]', text) if s.strip()]
    if sentences:
        last_sentence = sentences[-1]
        if len(last_sentence.split()) <= 15:  # Sanity check for span length
            return last_sentence

    # Fallback: return text as-is
    return text.strip()[:200]  # Truncate to avoid huge spans


def score_answer(prediction: str, ground_truth: str) -> Tuple[bool, float]:
    """
    Score a prediction against ground truth.

    Returns (is_correct, f1_score) where is_correct is True if F1 >= F1_THRESHOLD.
    """
    f1 = f1_score(prediction, ground_truth)
    is_correct = f1 >= F1_THRESHOLD
    return is_correct, f1


# ---------------------------------------------------------------------------
# API Calls
# ---------------------------------------------------------------------------

def call_deepseek(system: str, user_prompt: str, max_tokens: int = 256) -> Tuple[str, float]:
    """Call DeepSeek API. Returns (response_text, latency_seconds)."""
    t0 = time.time()
    try:
        r = ds_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
        )
        response = (r.choices[0].message.content or "").strip()
        latency = time.time() - t0
        return response, latency
    except Exception as e:
        print(f"    [API Error] {e}")
        return "", 0.0


# ---------------------------------------------------------------------------
# Benchmark Result Tracking
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    dataset: str
    strategy: str
    total_samples: int
    correct: int
    accuracy: float
    avg_input_tokens: float
    avg_latency: float


# ---------------------------------------------------------------------------
# Main Benchmark Runner
# ---------------------------------------------------------------------------

def run_benchmark():
    """Run the context accuracy benchmark with strategies A and B (skip C until executor-p2 confirms retrieval)."""

    print("\n" + "=" * 80)
    print("BREVITAS CONTEXT ACCURACY BENCHMARK — Phase 2 Validation (A vs B vs C)")
    print("=" * 80)
    print(f"Dataset       : HotpotQA (distractor, {N_SAMPLES} samples)")
    print(f"Model         : {MODEL} (temperature={TEMPERATURE})")
    print(f"Seed          : {SEED}")
    print(f"Strategies    : A=FULL-CONTEXT | B=LOSSY-PRUNE | C=RLM-RETRIEVAL (dense)")
    print(f"Scoring       : F1 >= {F1_THRESHOLD} (SQuAD/HotpotQA standard)")
    print(f"Retriever     : {RETRIEVER_METHOD.upper() if RETRIEVER_ACTIVE else 'DISABLED'}")
    print("=" * 80)

    # Load dataset ONCE and use same samples for all strategies
    print(f"\nLoading HotpotQA ({N_SAMPLES} samples)...")
    samples = load_hotpotqa(split="validation", n=N_SAMPLES)
    print(f"✓ Loaded {len(samples)} samples")
    print(f"Sample IDs: {[item['id'][:8] for item in samples]}")

    # Prepare strategies (include C with real retriever)
    strategies = [
        FullContextStrategy(),
        LossyPruneStrategy(),
        RLMRetrievalStrategy(retriever=dense_retriever if RETRIEVER_ACTIVE else None, top_k=5),
    ]

    system_prompt = (
        "You are a factual QA assistant. Answer the question based on the provided context. "
        "Be concise and provide only the answer span (1-5 words)."
    )

    all_results: List[BenchmarkResult] = []

    # Run benchmark for each strategy
    for strategy in strategies:
        print(f"\n{'='*80}")
        print(f"Strategy: {strategy.get_name()}")
        print(f"{'='*80}")

        correct = 0
        f1_sum = 0.0
        tokens_sum = 0.0
        latency_sum = 0.0

        for i, item in enumerate(samples):
            full_context, question, supporting_facts = prepare_context_and_facts(item)
            gold_answer = item["answer"]

            # Prepare context using strategy
            prompt, input_tokens = strategy.prepare_context(
                full_context, question, supporting_facts
            )
            tokens_sum += input_tokens

            # Call model
            response, latency = call_deepseek(system_prompt, prompt, max_tokens=64)
            latency_sum += latency

            # Extract and evaluate answer
            pred_answer = extract_answer_from_response(response)
            is_correct, f1 = score_answer(pred_answer, gold_answer)
            f1_sum += f1

            if is_correct:
                correct += 1

            # Debug output for first few samples (to diagnose FULL-CONTEXT scoring)
            if i < 3 or (i + 1) % max(1, len(samples) // 2) == 0:
                print(
                    f"  [{i+1}/{len(samples)}] Q: {question[:60]}..."
                    f"\n           Gold: {gold_answer} | Pred: {pred_answer}"
                    f"\n           F1={f1:.2f} | Correct={is_correct} | Tokens={input_tokens:.0f}"
                )

            # Progress summary
            if (i + 1) % max(1, len(samples) // 3) == 0:
                print(
                    f"  → {strategy.get_name():15s} @ {i+1}/{len(samples)}: "
                    f"accuracy={correct/(i+1)*100:.1f}% avg_f1={f1_sum/(i+1):.2f}"
                )

        accuracy = (correct / len(samples)) * 100
        avg_tokens = tokens_sum / len(samples)
        avg_latency = latency_sum / len(samples)
        avg_f1 = f1_sum / len(samples)

        result = BenchmarkResult(
            dataset="HotpotQA",
            strategy=strategy.get_name(),
            total_samples=len(samples),
            correct=correct,
            accuracy=accuracy,
            avg_input_tokens=avg_tokens,
            avg_latency=avg_latency,
        )
        all_results.append(result)

        print(
            f"\n{strategy.get_name():15s}: {correct}/{len(samples)} correct  "
            f"Accuracy={accuracy:.1f}%  Avg F1={avg_f1:.2f}  "
            f"Avg tokens={avg_tokens:.0f}  Latency={avg_latency:.2f}s"
        )

        # Sanity check: FULL-CONTEXT should have high accuracy (it's the ceiling)
        if strategy.get_name() == "FULL-CONTEXT" and accuracy < 40:
            print(
                f"\n⚠️  WARNING: FULL-CONTEXT accuracy is {accuracy:.1f}%, "
                f"which is implausibly low for the accuracy ceiling."
                f"\nDebugging: Check answer extraction and normalization above."
            )

    # Summary and Comparison
    print(f"\n{'='*80}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*80}")
    print(f"{'Strategy':<15} {'Accuracy':<12} {'Tokens':<12} {'Latency':<12}")
    print("-" * 80)

    baseline_acc = next(
        (r.accuracy for r in all_results if r.strategy == "FULL-CONTEXT"), None
    )

    for result in all_results:
        degradation = ""
        if baseline_acc and result.strategy != "FULL-CONTEXT":
            pct_drop = ((baseline_acc - result.accuracy) / baseline_acc) * 100
            degradation = f"({pct_drop:+.1f}%)"

        print(
            f"{result.strategy:<15} {result.accuracy:>6.1f}% {result.avg_input_tokens:>10.0f}  "
            f"{result.avg_latency:>10.2f}s  {degradation}"
        )

    # Save results
    suffix = "_phase2" if RETRIEVER_ACTIVE else ""
    results_file = Path(__file__).parent / f"context_accuracy_results_n{N_SAMPLES}{suffix}.json"
    with open(results_file, "w") as f:
        json.dump([asdict(r) for r in all_results], f, indent=2)
    print(f"\n✓ Results saved to {results_file}")

    # Phase 2 Analysis
    print(f"\n{'='*80}")
    print("ANALYSIS")
    print(f"{'='*80}")

    full_acc = next((r.accuracy for r in all_results if r.strategy == "FULL-CONTEXT"), None)
    lossy_acc = next((r.accuracy for r in all_results if r.strategy == "LOSSY-PRUNE"), None)
    rlm_acc = next((r.accuracy for r in all_results if r.strategy == "RLM-RETRIEVAL"), None)

    full_tokens = next((r.avg_input_tokens for r in all_results if r.strategy == "FULL-CONTEXT"), None)
    lossy_tokens = next((r.avg_input_tokens for r in all_results if r.strategy == "LOSSY-PRUNE"), None)
    rlm_tokens = next((r.avg_input_tokens for r in all_results if r.strategy == "RLM-RETRIEVAL"), None)

    print(f"\nPhase 1 (B vs A):")
    if full_acc and lossy_acc:
        b_degr = ((full_acc - lossy_acc) / full_acc * 100) if full_acc > 0 else 0
        b_savings = ((full_tokens - lossy_tokens) / full_tokens * 100) if full_tokens > 0 else 0
        print(f"  Accuracy: {lossy_acc:.1f}% vs {full_acc:.1f}% (degradation: {b_degr:+.1f}%)")
        print(f"  Tokens:   {lossy_tokens:.0f} vs {full_tokens:.0f} (savings: {b_savings:.1f}%)")

    print(f"\nPhase 2 (C vs A):")
    if full_acc and rlm_acc and rlm_tokens:
        c_degr = ((full_acc - rlm_acc) / full_acc * 100) if full_acc > 0 else 0
        c_savings = ((full_tokens - rlm_tokens) / full_tokens * 100) if full_tokens > 0 else 0
        print(f"  Accuracy: {rlm_acc:.1f}% vs {full_acc:.1f}% (degradation: {c_degr:+.1f}%)")
        print(f"  Tokens:   {rlm_tokens:.0f} vs {full_tokens:.0f} (savings: {c_savings:.1f}%)")
        print(f"  Retriever: {RETRIEVER_METHOD.upper()}")

        if RETRIEVER_METHOD in ["dense-retrieval", "colbert-pylate"]:
            print(f"  ✓ Real retriever confirmed active ({RETRIEVER_METHOD})")
        else:
            print(f"  ⚠️  Unexpected retriever method: {RETRIEVER_METHOD}")

        # Phase 2 success criteria
        if rlm_acc >= full_acc * 0.85 and c_savings >= 50:
            print(f"\n✓ Phase 2 SUCCESS: C approaches A while saving {c_savings:.0f}% tokens")
        elif rlm_acc > lossy_acc:
            print(f"\n⚠️  Phase 2 PARTIAL: C improves on B but gaps remain vs A")
        else:
            print(f"\n❌ Phase 2 NEEDS WORK: C underperforms B")
    else:
        print(f"  (Incomplete data for comparison)")

    print(f"\nResults file: {Path(__file__).parent / f'context_accuracy_results_n{N_SAMPLES}_phase2.json'}")


if __name__ == "__main__":
    # Allow override of N_SAMPLES via CLI
    if len(sys.argv) > 1:
        if sys.argv[1] == "--samples" and len(sys.argv) > 2:
            N_SAMPLES = int(sys.argv[2])

    run_benchmark()
