from statistics import mean
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from token_efficiency_model.combined_tactics.pipeline import TokenEfficientPipeline


def benchmark(turns: int = 60):
    messages = [
        "Architect: keep rollout gated by SLO and error budget.",
        "Builder: deploy in phased waves with automatic rollback.",
        "Reviewer: preserve audit trace and output strict JSON schema.",
    ]
    context = [
        f"Context-{i}: service contract, dependency map, and mitigation checklist."
        for i in range(10)
    ]

    baseline = TokenEfficientPipeline(quality_floor=0.98)
    delta = TokenEfficientPipeline(quality_floor=0.98)

    base_steady = []
    delta_steady = []
    base_savings = []
    delta_savings = []

    for turn in range(turns):
        base_result = baseline.process_task(
            task_text="Generate minimal-risk deployment plan and return only required fields.",
            incoming_messages=messages,
            prior_context=context,
            task_id=f"b-{turn}",
            complexity=0.55,
            urgency=0.65,
            compression_level=2,
            prune_budget=3,
            protocol_mode="compact",
            delta_mode="off",
            delta_aggressiveness=1,
            wire_mode="json",
        )
        delta_result = delta.process_task(
            task_text="Generate minimal-risk deployment plan and return only required fields.",
            incoming_messages=messages,
            prior_context=context,
            task_id=f"d-{turn}",
            complexity=0.55,
            urgency=0.65,
            compression_level=2,
            prune_budget=3,
            protocol_mode="compact",
            delta_mode="state-delta",
            delta_aggressiveness=3,
            wire_mode="binary",
        )

        base_steady.append(base_result.steady_state_tokens)
        delta_steady.append(delta_result.steady_state_tokens)
        base_savings.append(base_result.savings_pct)
        delta_savings.append(delta_result.savings_pct)

    warm_start = min(5, turns)
    base_after_warmup = base_steady[warm_start:]
    delta_after_warmup = delta_steady[warm_start:]

    mean_base = mean(base_after_warmup)
    mean_delta = mean(delta_after_warmup)
    drop_pct = (1.0 - (mean_delta / mean_base)) * 100.0 if mean_base > 0 else 0.0

    print("=== Delta Benchmark (Forced Modes) ===")
    print(f"Turns: {turns}")
    print(f"Baseline steady tokens (post-warmup): {mean_base:.2f}")
    print(f"Delta steady tokens (post-warmup): {mean_delta:.2f}")
    print(f"Steady token drop from delta mode: {drop_pct:.2f}%")
    print(f"Baseline avg savings (%): {mean(base_savings):.2f}")
    print(f"Delta avg savings (%): {mean(delta_savings):.2f}")


if __name__ == "__main__":
    benchmark(turns=60)
