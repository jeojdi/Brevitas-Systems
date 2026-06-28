import argparse
import random
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from token_efficiency_model.combined_tactics import RLTokenOrchestrator, TokenEfficientPipeline
from token_efficiency_model.combined_tactics.rl_orchestrator import RLStep
from token_efficiency_model.common.metrics import quality_floor_penalty


def synthetic_task(index: int, thread_id: int):
    complexity = random.random()
    urgency = random.random()
    context_count = random.randint(4, 14)
    msg_count = random.randint(2, 8)

    task_text = (
        f"Task {index} thread-{thread_id}: analyze architecture constraints, optimize rollout plan, "
        f"and report only critical risks."
    )
    incoming_messages = [
        f"Agent-{i}: We observed repeated handoff context and redundant details in subsystem {i % 3}."
        for i in range(msg_count)
    ]
    prior_context = [
        f"Context-{j}: Prior decision about api contract, deployment dependencies, and monitoring policy {j}."
        for j in range(context_count)
    ]
    context_load = min(1.0, (context_count + msg_count) / 20.0)

    continuity = min(1.0, 0.25 + (thread_id % 5) * 0.15)

    return {
        "task_text": task_text,
        "incoming_messages": incoming_messages,
        "prior_context": prior_context,
        "complexity": complexity,
        "urgency": urgency,
        "context_load": context_load,
        "continuity": continuity,
        "thread_id": thread_id,
    }


def compute_reward(
    savings_pct: float,
    quality_proxy: float,
    steady_state_tokens_value: int,
    delta_mode: str,
    continuity: float,
    floor: float = 0.98,
) -> float:
    base = 0.65 * (savings_pct / 100.0) + 0.35 * quality_proxy
    token_bonus = max(0.0, 1.0 - min(1.0, steady_state_tokens_value / 120.0)) * 0.25
    continuity_bonus = 0.08 if delta_mode == "state-delta" and continuity >= 0.45 else 0.0
    return base + token_bonus + continuity_bonus - quality_floor_penalty(quality_proxy, floor=floor)


def run(episodes: int, savings_target: float = 70.0, max_tuning: int = 3, do_plot: bool = False):
    orchestrator = RLTokenOrchestrator()
    persistence_file = ROOT / "experiments" / ".delta_memory_store.json"
    # Enable tuning to try to reach 70% savings target by default
    pipeline = TokenEfficientPipeline(
        memory_persistence_path=str(persistence_file),
        quality_floor=0.98,
        savings_target=savings_target,
        max_tuning_attempts=max_tuning,
    )

    rewards = []
    savings = []
    quality_scores = []
    steady_state_tokens = []
    cold_start_tokens = []
    cache_hit_rates = []
    rehydration_events = []
    diversity_scores = []
    novelty_gains = []
    pareto_decisions = 0
    episodes_axis = []

    for episode in range(episodes):
        episodes_axis.append(episode + 1)
        thread_id = max(1, episode // 4)
        task = synthetic_task(episode, thread_id=thread_id)
        prior_cache_hit = cache_hit_rates[-1] if cache_hit_rates else 0.0
        state = orchestrator.discretize_state(
            task["complexity"],
            task["urgency"],
            task["context_load"],
            cache_hit_rate=prior_cache_hit,
            continuity=task["continuity"],
        )

        action_idx, config = orchestrator.select_action(
            state,
            explore=True,
            metrics={
                "quality": quality_scores[-1] if quality_scores else 1.0,
                "savings": savings[-1] if savings else 0.0,
            },
        )
        result = pipeline.process_task(
            task_text=task["task_text"],
            incoming_messages=task["incoming_messages"],
            prior_context=task["prior_context"],
            task_id=f"ep-{episode}",
            complexity=task["complexity"],
            urgency=task["urgency"],
            compression_level=config.compression_level,
            prune_budget=config.prune_budget,
            protocol_mode=config.protocol_mode,
            delta_mode=config.delta_mode,
            delta_aggressiveness=config.delta_aggressiveness,
            wire_mode=config.wire_mode,
        )

        reward = compute_reward(
            result.savings_pct,
            result.quality_proxy,
            result.steady_state_tokens,
            config.delta_mode,
            task["continuity"],
            floor=0.98,
        )

        next_task = synthetic_task(episode + 1, thread_id=max(1, (episode + 1) // 4))
        next_state = orchestrator.discretize_state(
            next_task["complexity"],
            next_task["urgency"],
            next_task["context_load"],
            cache_hit_rate=float(result.debug.get("cache_hit_rate", 0.0)),
            continuity=next_task["continuity"],
        )
        orchestrator.update(RLStep(state=state, action_idx=action_idx, reward=reward, next_state=next_state))

        rewards.append(reward)
        savings.append(result.savings_pct)
        quality_scores.append(result.quality_proxy)
        steady_state_tokens.append(result.steady_state_tokens)
        cold_start_tokens.append(result.cold_start_tokens)
        cache_hit_rates.append(float(result.debug.get("cache_hit_rate", 0.0)))
        rehydration_events.append(int(result.debug.get("rehydration_events", 0)))

        sampling_debug = result.debug.get("adaptive_sampling", {})
        diversity_scores.append(float(sampling_debug.get("diversity_score", 0.0)))
        novelty_gains.append(float(sampling_debug.get("average_novelty_gain", 0.0)))
        if orchestrator.last_selected_reason == "pareto_frontier":
            pareto_decisions += 1

    print("=== RL Token Efficiency Simulation ===")
    print(f"Episodes: {episodes}")
    print(f"Avg Reward: {mean(rewards):.4f}")
    print(f"Avg Token Savings (%): {mean(savings):.2f}")
    print(f"Avg Quality Proxy: {mean(quality_scores):.4f}")
    print(f"Avg Steady-State Tokens: {mean(steady_state_tokens):.2f}")
    print(f"Avg Cold-Start Tokens: {mean(cold_start_tokens):.2f}")
    print(f"Avg Cache Hit Rate: {mean(cache_hit_rates):.3f}")
    print(f"Total Rehydration Events: {sum(rehydration_events)}")
    print(f"Avg Diversity Score: {mean(diversity_scores):.3f}")
    print(f"Avg Novelty Gain: {mean(novelty_gains):.3f}")
    print(f"Pareto Frontier Decisions: {pareto_decisions}/{episodes}")

    # Optional plotting / telemetry
    telemetry_dir = ROOT / "experiments"
    try:
        from token_efficiency_model.experiments.telemetry import save_metrics_csv
        save_metrics_csv(
            str(telemetry_dir / "metrics_simulation.csv"),
            episodes_axis,
            {
                "reward": rewards,
                "savings_pct": savings,
                "quality_proxy": quality_scores,
                "steady_state_tokens": steady_state_tokens,
                "cold_start_tokens": cold_start_tokens,
                "cache_hit_rate": cache_hit_rates,
                "rehydration_events": rehydration_events,
                "diversity_score": diversity_scores,
                "novelty_gain": novelty_gains,
            },
        )
        print("Saved telemetry CSV to experiments/metrics_simulation.csv")
    except Exception:
        print("Telemetry CSV could not be written.")

    if do_plot:
        try:
            from token_efficiency_model.experiments.telemetry import plot_savings
            plot_savings(episodes_axis, savings, str(telemetry_dir / "metrics.png"))
            print("Saved plot to experiments/metrics.png")
        except Exception:
            print("Plotting failed or matplotlib not installed; metrics CSV not saved.")

    print("\nSample learned policy (first 5 states):")
    shown = 0
    for state, q_values in orchestrator.q_table.items():
        if shown >= 5:
            break
        best_idx = int(q_values.argmax())
        best = orchestrator.actions[best_idx]
        print(
            f"State={state} -> compression={best.compression_level}, prune_budget={best.prune_budget}, "
            f"protocol={best.protocol_mode}, delta={best.delta_mode}, "
            f"aggr={best.delta_aggressiveness}, wire={best.wire_mode}"
        )
        shown += 1


def parse_args():
    parser = argparse.ArgumentParser(description="Train and evaluate token-efficiency RL orchestrator")
    parser.add_argument("--episodes", type=int, default=200, help="Number of RL episodes")
    parser.add_argument("--savings-target", type=float, default=70.0, help="Savings target percentage for pipeline tuning")
    parser.add_argument("--max-tuning", type=int, default=3, help="Max tuning attempts per task")
    parser.add_argument("--plot", action="store_true", help="Save a simple savings plot to experiments/metrics.png")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.episodes, savings_target=args.savings_target, max_tuning=args.max_tuning, do_plot=args.plot)
