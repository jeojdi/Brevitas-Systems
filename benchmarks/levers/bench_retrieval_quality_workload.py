"""Paired full-context vs production-order retrieval quality workload.

Runs real DeepSeek answers over HotpotQA distractor examples and reports paired
help/harm rates, official EM/F1, supporting-title recall, and prompt-token savings.
The retrieval arm uses the same FastEmbed MiniLM adapter and quality-first hybrid
RRF + bridge-hop configuration as the production proxy. Selected passages are
restored to original message order, matching ``engine.optimize_request``.

Usage:
    python benchmarks/levers/bench_retrieval_quality_workload.py 50
    python benchmarks/levers/bench_retrieval_quality_workload.py 50 \
      --baseline-results benchmarks/levers/results_retrieval_quality_workload_n50.json

Environment:
    BREVITAS_HOTPOT_PATH  Optional path to HotpotQA distractor JSON.
    DEEPSEEK_API_KEY      Loaded from the environment or .env.local.
"""

from __future__ import annotations

import json
import argparse
import os
import random
import re
import string
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from token_efficiency_model.lossless.api_adapter import retrieval_select


DEFAULT_DATASET = Path("/private/tmp/hotpot_dev_distractor_v1.json")
DATASET_URL = "http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json"


def normalize_answer(value: str) -> str:
    def remove_articles(text: str) -> str:
        return re.sub(r"\b(a|an|the)\b", " ", text)

    def remove_punctuation(text: str) -> str:
        return "".join(char for char in text if char not in set(string.punctuation))

    return " ".join(remove_articles(remove_punctuation(value.lower())).split())


def exact_match(prediction: str, gold: str) -> float:
    return float(normalize_answer(prediction) == normalize_answer(gold))


def f1_score(prediction: str, gold: str) -> float:
    predicted = normalize_answer(prediction).split()
    expected = normalize_answer(gold).split()
    common = Counter(predicted) & Counter(expected)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(expected)
    return 2 * precision * recall / (precision + recall)


def load_env_key() -> str:
    for name in ("DEEPSEEK_API_KEY", "Deepseek_api_key"):
        if os.getenv(name):
            return os.environ[name]
    env_file = ROOT / ".env.local"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            name, value = line.split("=", 1)
            if name.strip() in ("DEEPSEEK_API_KEY", "Deepseek_api_key") and value.strip():
                return value.strip()
    raise SystemExit("DEEPSEEK_API_KEY is not configured")


def load_dataset(count: int) -> list[dict]:
    path = Path(os.getenv("BREVITAS_HOTPOT_PATH", str(DEFAULT_DATASET)))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(DATASET_URL, path)
    rows = json.loads(path.read_text())
    return rows[: min(count, len(rows))]


def ask_deepseek(context: str, question: str, key: str, retries: int = 4) -> tuple[str, dict]:
    prompt = (
        "Answer the question using ONLY the context. Reply with the shortest exact answer "
        "(a name, entity, yes/no, or number). No explanation.\n\n"
        f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
    )
    body = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "temperature": 0.0,
    }
    request = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read())
            answer = payload["choices"][0]["message"]["content"].strip()
            return answer, payload.get("usage", {})
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("DeepSeek request exhausted retries")


def passages_for(row: dict) -> tuple[list[str], list[str]]:
    passages = []
    titles = []
    for title, sentences in row["context"]:
        titles.append(title)
        passages.append(f"{title}. {' '.join(sentences)}")
    return passages, titles


def restore_original_order(original: list[str], selected: list[str]) -> list[str]:
    remaining = Counter(selected)
    ordered = []
    for passage in original:
        if remaining[passage] > 0:
            ordered.append(passage)
            remaining[passage] -= 1
    return ordered


def mean_percent(values: list[float]) -> float:
    return round(100 * sum(values) / len(values), 2) if values else 0.0


def paired_bootstrap(deltas: list[float], samples: int = 5000) -> list[float]:
    if not deltas:
        return [0.0, 0.0]
    rng = random.Random(42)
    count = len(deltas)
    means = sorted(
        100 * sum(deltas[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(samples)
    )
    return [round(means[int(samples * 0.025)], 2), round(means[int(samples * 0.975)], 2)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("count", nargs="?", type=int, default=50)
    parser.add_argument(
        "--baseline-results",
        type=Path,
        help="Reuse full-context answers/usage from a prior paired run; only call retrieval.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    count = args.count
    key = load_env_key()
    rows = load_dataset(count)
    records = []
    baseline_by_id = {}
    if args.baseline_results:
        baseline_payload = json.loads(args.baseline_results.read_text())
        baseline_by_id = {
            record.get("id"): record for record in baseline_payload.get("records", [])
        }

    for index, row in enumerate(rows):
        passages, titles = passages_for(row)
        question = row["question"]
        gold = row["answer"]

        baseline = baseline_by_id.get(row.get("_id"))
        if baseline:
            full_answer = baseline["full_answer"]
            full_usage = {"prompt_tokens": baseline["full_prompt_tokens"]}
        else:
            full_answer, full_usage = ask_deepseek("\n\n".join(passages), question, key)
        selection = retrieval_select(question, passages, k=8, use_adaptive=True)
        selected = restore_original_order(passages, selection["selected_context"])
        retrieval_answer, retrieval_usage = ask_deepseek("\n\n".join(selected), question, key)

        full_em = exact_match(full_answer, gold)
        retrieval_em = exact_match(retrieval_answer, gold)
        full_f1 = f1_score(full_answer, gold)
        retrieval_f1 = f1_score(retrieval_answer, gold)
        selected_titles = {titles[i] for i, passage in enumerate(passages) if passage in set(selected)}
        supporting_titles = {fact[0] for fact in row.get("supporting_facts", [])}

        records.append({
            "index": index,
            "id": row.get("_id", ""),
            "question": question,
            "gold": gold,
            "full_answer": full_answer,
            "retrieval_answer": retrieval_answer,
            "full_em": full_em,
            "retrieval_em": retrieval_em,
            "full_f1": full_f1,
            "retrieval_f1": retrieval_f1,
            "full_prompt_tokens": int(full_usage.get("prompt_tokens", 0)),
            "retrieval_prompt_tokens": int(retrieval_usage.get("prompt_tokens", 0)),
            "passages_sent": len(selected),
            "supporting_titles": sorted(supporting_titles),
            "selected_supporting_titles": sorted(supporting_titles & selected_titles),
            "supporting_title_recall": (
                len(supporting_titles & selected_titles) / len(supporting_titles)
                if supporting_titles else 1.0
            ),
            "fallback_applied": selection["fallback_applied"],
            "retrieval_reason": selection["reason"],
            "retrieval_method": selection.get("method"),
            "bridge_expansions": selection.get("bridge_expansions", 0),
            "quality_status": selection.get("quality_status"),
        })
        outcome = "same"
        if full_em > retrieval_em:
            outcome = "hurt"
        elif retrieval_em > full_em:
            outcome = "helped"
        print(
            f"[{index + 1:02d}/{len(rows)}] {outcome:6s} "
            f"full={full_answer[:28]!r} retrieval={retrieval_answer[:28]!r} "
            f"k={len(selected)}",
            flush=True,
        )

    full_em = [record["full_em"] for record in records]
    retrieval_em = [record["retrieval_em"] for record in records]
    full_f1 = [record["full_f1"] for record in records]
    retrieval_f1 = [record["retrieval_f1"] for record in records]
    em_deltas = [retrieval - full for retrieval, full in zip(retrieval_em, full_em)]
    f1_deltas = [retrieval - full for retrieval, full in zip(retrieval_f1, full_f1)]
    full_tokens = sum(record["full_prompt_tokens"] for record in records)
    retrieval_tokens = sum(record["retrieval_prompt_tokens"] for record in records)

    summary = {
        "n_questions": len(records),
        "dataset": "HotpotQA distractor validation",
        "model": "deepseek-chat",
        "retrieval": "production-order hybrid dense+BM25 RRF with bounded bridge hop, k=8",
        "full_context_reused": bool(baseline_by_id),
        "full_context": {
            "EM": mean_percent(full_em),
            "F1": mean_percent(full_f1),
            "prompt_tokens": full_tokens,
        },
        "adaptive_retrieval": {
            "EM": mean_percent(retrieval_em),
            "F1": mean_percent(retrieval_f1),
            "prompt_tokens": retrieval_tokens,
        },
        "EM_delta_points": round(mean_percent(retrieval_em) - mean_percent(full_em), 2),
        "F1_delta_points": round(mean_percent(retrieval_f1) - mean_percent(full_f1), 2),
        "EM_delta_95pct_paired_bootstrap_CI": paired_bootstrap(em_deltas),
        "F1_delta_95pct_paired_bootstrap_CI": paired_bootstrap(f1_deltas),
        "token_reduction_pct": round(100 * (1 - retrieval_tokens / max(1, full_tokens)), 2),
        "full_correct_retrieval_wrong": sum(delta < 0 for delta in em_deltas),
        "full_wrong_retrieval_correct": sum(delta > 0 for delta in em_deltas),
        "answers_changed": sum(
            normalize_answer(record["full_answer"]) != normalize_answer(record["retrieval_answer"])
            for record in records
        ),
        "questions_missing_supporting_title": sum(
            record["supporting_title_recall"] < 1.0 for record in records
        ),
        "supporting_title_recall_pct": round(
            100 * sum(record["supporting_title_recall"] for record in records) / len(records), 2
        ),
        "retrieval_fallbacks": sum(record["fallback_applied"] for record in records),
        "bridge_expansions": sum(record["bridge_expansions"] for record in records),
        "average_passages_sent": round(
            sum(record["passages_sent"] for record in records) / len(records), 2
        ),
    }
    output = Path(__file__).with_name(
        f"results_retrieval_quality_workload_hybrid_n{len(records)}.json"
    )
    output.write_text(json.dumps({"summary": summary, "records": records}, indent=2) + "\n")
    print("\n" + json.dumps(summary, indent=2), flush=True)
    print(f"\nSaved {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
