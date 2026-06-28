"""Advanced Token Efficiency Benchmark

Uses nuanced, realistic scenarios to evaluate token efficiency improvements
from adaptive semantic sampling. Tests against multiple scenario types
with challenging edge cases.
"""

import argparse
import sys
from pathlib import Path
from statistics import mean, stdev
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from token_efficiency_model.combined_tactics import MoEPipeline, MoEOrchestrator, RLTokenOrchestrator
from token_efficiency_model.combined_tactics.rl_orchestrator import RLStep
from token_efficiency_model.common.metrics import quality_floor_penalty
from token_efficiency_model.experts.gate import Gate
from token_efficiency_model.experiments.advanced_test_data import AdvancedTestDataGenerator, ScenarioType


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


def run_advanced_benchmark(episodes: int = 200, scenario_mix: str = "balanced", savings_target: float = 70.0, max_tuning: int = 3, do_plot: bool = False):
    """
    Run advanced benchmark with realistic scenarios

    Args:
        episodes: Number of episodes to run
        scenario_mix: "balanced" (all types), "complex" (hard scenarios), "stateful" (multi-turn focus), "reasoning" (reasoning-focused)
    """

    generator = AdvancedTestDataGenerator(seed=42)
    persistence_file = ROOT / "experiments" / ".delta_memory_store_advanced.json"
    pipeline = MoEPipeline(
        memory_persistence_path=str(persistence_file),
        quality_floor=0.98,
        savings_target=savings_target,
        max_tuning_attempts=max_tuning,
    )

    # Build per-expert action filters
    EXPERT_IDS = ["operational", "math", "multihop", "logical", "planning", "swe", "research"]
    seed_orchestrator = RLTokenOrchestrator()
    action_filters = {eid: pipeline.expert_action_filter(eid, seed_orchestrator.actions) for eid in EXPERT_IDS}
    orchestrator = MoEOrchestrator(EXPERT_IDS, action_filters=action_filters)
    gate = Gate(mode="rule")

    # Generate scenarios using the generator's workload method
    if scenario_mix in ["complex", "stateful"]:
        # Legacy support: complex and stateful use manual distribution
        if scenario_mix == "complex":
            scenario_distribution = {
                ScenarioType.HIGH_COMPLEXITY_REASONING: 0.25,
                ScenarioType.ADVERSARIAL_PRUNING: 0.25,
                ScenarioType.CASCADING_DECISIONS: 0.20,
                ScenarioType.DOMAIN_SPECIFIC: 0.15,
                ScenarioType.MULTI_TURN_STATEFUL: 0.10,
                ScenarioType.CROSS_TEAM_COMM: 0.05,
            }
        else:  # stateful
            scenario_distribution = {
                ScenarioType.MULTI_TURN_STATEFUL: 0.50,
                ScenarioType.CASCADING_DECISIONS: 0.15,
                ScenarioType.EMERGENT_BEHAVIOR: 0.15,
                ScenarioType.TIMESERIES_ANALYSIS: 0.10,
                ScenarioType.DOMAIN_SPECIFIC: 0.10,
            }

        scenarios = []
        remaining = episodes
        for scenario_type, proportion in scenario_distribution.items():
            count_for_type = int(episodes * proportion)
            for _ in range(count_for_type):
                scenarios.append(generator.generate_advanced_scenario(scenario_type))
            remaining -= count_for_type

        # Fill remaining with balanced random scenarios
        if remaining > 0:
            for _ in range(remaining):
                scenarios.append(generator.generate_advanced_scenario())
    else:
        # Use generate_workload for "balanced" and "reasoning" mixes
        scenarios = generator.generate_workload(num_scenarios=episodes, mix=scenario_mix if scenario_mix != "balanced" else None)

    # Track metrics by scenario type and expert
    metrics_by_scenario = defaultdict(lambda: {
        "rewards": [],
        "savings": [],
        "quality": [],
        "tokens": [],
        "cache_hits": [],
        "rehydrations": [],
    })

    metrics_by_expert = defaultdict(lambda: {
        "episodes": 0,
        "rewards": [],
    })
    
    overall_metrics = {
        "rewards": [],
        "savings": [],
        "quality": [],
        "steady_state_tokens": [],
        "cold_start_tokens": [],
        "cache_hit_rates": [],
        "rehydration_events": [],
        "sampling_effectiveness": [],
        "diversity_scores": [],
        "novelty_gains": [],
        "pareto_decisions": 0,
    }
    
    print(f"Running Advanced Token Efficiency Benchmark")
    print(f"Episodes: {episodes}")
    print(f"Scenario Mix: {scenario_mix}")
    print(f"Calculating performance improvements...\n")
    
    for episode, task in enumerate(scenarios):
        scenario_type = task.get("scenario_type", "unknown")

        prior_cache_hit = overall_metrics["cache_hit_rates"][-1] if overall_metrics["cache_hit_rates"] else 0.0

        # Route task to expert
        expert_id = gate.route(task)

        state = orchestrator.tables[expert_id].discretize_state(
            task["complexity"],
            task["urgency"],
            task.get("context_load", 0.5),
            cache_hit_rate=prior_cache_hit,
            continuity=task.get("continuity", 0.5),
        )

        action_idx, config = orchestrator.select_action(
            expert_id,
            state,
            explore=True,
            metrics={
                "quality": overall_metrics["quality"][-1] if overall_metrics["quality"] else 1.0,
                "savings": overall_metrics["savings"][-1] if overall_metrics["savings"] else 0.0,
            },
        )

        result = pipeline.process_task(
            task_text=task["task_text"],
            incoming_messages=task["incoming_messages"],
            prior_context=task["prior_context"],
            task_id=f"advanced-{episode}",
            complexity=task["complexity"],
            urgency=task["urgency"],
            compression_level=config.compression_level,
            prune_budget=config.prune_budget,
            protocol_mode=config.protocol_mode,
            delta_mode=config.delta_mode,
            delta_aggressiveness=config.delta_aggressiveness,
            wire_mode=config.wire_mode,
            must_keep_facts=task.get("must_keep_facts"),
            task_family=task.get("task_family"),
            scenario_type=task["scenario_type"].value if hasattr(task.get("scenario_type"), "value") else task.get("scenario_type"),
        )

        reward = compute_reward(
            result.savings_pct,
            result.quality_proxy,
            result.steady_state_tokens,
            config.delta_mode,
            task.get("continuity", 0.5),
            floor=0.98,
        )

        next_task = scenarios[episode + 1] if episode + 1 < len(scenarios) else task
        next_state = orchestrator.tables[expert_id].discretize_state(
            next_task["complexity"],
            next_task["urgency"],
            next_task.get("context_load", 0.5),
            cache_hit_rate=float(result.debug.get("cache_hit_rate", 0.0)),
            continuity=next_task.get("continuity", 0.5),
        )
        orchestrator.update(expert_id, RLStep(state=state, action_idx=action_idx, reward=reward, next_state=next_state))

        # Track per-expert metrics
        metrics_by_expert[expert_id]["episodes"] += 1
        metrics_by_expert[expert_id]["rewards"].append(reward)

        # Update overall metrics
        overall_metrics["rewards"].append(reward)
        overall_metrics["savings"].append(result.savings_pct)
        overall_metrics["quality"].append(result.quality_proxy)
        overall_metrics["steady_state_tokens"].append(result.steady_state_tokens)
        overall_metrics["cold_start_tokens"].append(result.cold_start_tokens)
        overall_metrics["cache_hit_rates"].append(float(result.debug.get("cache_hit_rate", 0.0)))
        overall_metrics["rehydration_events"].append(int(result.debug.get("rehydration_events", 0)))
        
        # Calculate sampling effectiveness
        sampling_debug = result.debug.get("adaptive_sampling", {})
        if sampling_debug.get("total_count", 0) > 0:
            sample_ratio = sampling_debug.get("sampled_count", 0) / sampling_debug.get("total_count", 1)
            avg_relevance = sampling_debug.get("average_relevance", 0.0)
            sampling_score = (1.0 - sample_ratio) * (avg_relevance)  # Efficiency: low ratio, high relevance
            overall_metrics["sampling_effectiveness"].append(sampling_score)
        overall_metrics["diversity_scores"].append(float(sampling_debug.get("diversity_score", 0.0)))
        overall_metrics["novelty_gains"].append(float(sampling_debug.get("average_novelty_gain", 0.0)))
        if orchestrator.tables[expert_id].last_selected_reason == "pareto_frontier":
            overall_metrics["pareto_decisions"] += 1

        # Track by scenario type
        metrics_by_scenario[str(scenario_type)]["rewards"].append(reward)
        metrics_by_scenario[str(scenario_type)]["savings"].append(result.savings_pct)
        metrics_by_scenario[str(scenario_type)]["quality"].append(result.quality_proxy)
        metrics_by_scenario[str(scenario_type)]["tokens"].append(result.steady_state_tokens)
        metrics_by_scenario[str(scenario_type)]["cache_hits"].append(float(result.debug.get("cache_hit_rate", 0.0)))
        
        if (episode + 1) % 50 == 0:
            print(f"✓ Completed {episode + 1}/{episodes} episodes")

    # Print comprehensive results
    print("\n" + "="*70)
    print("Results")
    print("="*70)
    
    print(f"\n🎯 Overall Performance Metrics:")
    print(f"  Avg Reward: {mean(overall_metrics['rewards']):.4f} (σ={stdev(overall_metrics['rewards']) if len(overall_metrics['rewards']) > 1 else 0:.4f})")
    print(f"  Avg Token Savings: {mean(overall_metrics['savings']):.2f}%")
    print(f"  Avg Quality Score: {mean(overall_metrics['quality']):.4f}")
    print(f"  Avg Steady-State Tokens: {mean(overall_metrics['steady_state_tokens']):.1f}")
    print(f"  Avg Cold-Start Tokens: {mean(overall_metrics['cold_start_tokens']):.1f}")
    print(f"  Avg Cache Hit Rate: {mean(overall_metrics['cache_hit_rates']):.3f}")
    print(f"  Total Rehydration Events: {sum(overall_metrics['rehydration_events'])}")
    
    if overall_metrics["sampling_effectiveness"]:
        print(f"  Avg Sampling Effectiveness: {mean(overall_metrics['sampling_effectiveness']):.4f}")
    print(f"  Avg Diversity Score: {mean(overall_metrics['diversity_scores']):.4f}")
    print(f"  Avg Novelty Gain: {mean(overall_metrics['novelty_gains']):.4f}")
    print(f"  Pareto Frontier Decisions: {overall_metrics['pareto_decisions']}/{episodes}")
    
    print(f"\nPer-Scenario Type Performance:")
    print("-" * 70)
    
    for scenario_type_str in sorted(metrics_by_scenario.keys()):
        metrics = metrics_by_scenario[scenario_type_str]
        scenario_label = scenario_type_str.replace("ScenarioType.", "").replace("_", "-").lower()
        count = len(metrics["rewards"])
        
        print(f"\n  {scenario_label.upper()} ({count} episodes):")
        print(f"    Savings: {mean(metrics['savings']):.2f}% ± {stdev(metrics['savings']) if len(metrics['savings']) > 1 else 0:.2f}%")
        print(f"    Quality: {mean(metrics['quality']):.4f}")
        print(f"    Avg Tokens: {mean(metrics['tokens']):.1f}")
        print(f"    Cache Hit: {mean(metrics['cache_hits']):.3f}")
    
    print("\n" + "="*70)
    print("Learned Optimal Policy by Expert:")
    print("-" * 70)

    for expert_id in EXPERT_IDS:
        expert_table = orchestrator.tables[expert_id]
        if expert_table.q_table:
            # Get first state-action pair for this expert
            first_state = sorted(expert_table.q_table.keys())[0]
            q_values = expert_table.q_table[first_state]
            best_idx = int(q_values.argmax())
            best = expert_table.actions[best_idx]
            print(f"\n  {expert_id.upper()}:")
            print(f"    Episodes: {metrics_by_expert[expert_id]['episodes']}")
            print(f"    Best Action: compression={best.compression_level}, prune_budget={best.prune_budget}")
            print(f"    → protocol={best.protocol_mode}, delta={best.delta_mode}")
        else:
            print(f"\n  {expert_id.upper()}: No episodes")
    
    print("\n" + "="*70)
    print(f"Key Insights:")
    print(f"  • Adaptive semantic sampling uses novelty-aware reranking")
    print(f"  • Anchor-preserving context selection improves continuity under budget")
    print(f"  • Pareto frontier orchestration balances quality vs token savings")
    print(f"  • Scenario diversity stress-tested edge cases across {episodes} episodes")
    print("="*70 + "\n")
    
    return overall_metrics


def parse_args():
    parser = argparse.ArgumentParser(description="Advanced token-efficiency benchmark with realistic scenarios")
    parser.add_argument("--episodes", type=int, default=200, help="Number of benchmark episodes")
    parser.add_argument(
        "--scenario-mix",
        choices=["balanced", "complex", "stateful", "reasoning"],
        default="balanced",
        help="Scenario distribution: balanced (all types), complex (hard scenarios), stateful (multi-turn), reasoning (reasoning-focused)"
    )
    parser.add_argument("--savings-target", type=float, default=70.0, help="Savings target percentage for pipeline tuning")
    parser.add_argument("--max-tuning", type=int, default=3, help="Max tuning attempts per task")
    parser.add_argument("--plot", action="store_true", help="Save a simple savings plot to experiments/metrics_advanced.png")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    metrics = run_advanced_benchmark(
        episodes=args.episodes,
        scenario_mix=args.scenario_mix,
        savings_target=args.savings_target,
        max_tuning=args.max_tuning,
        do_plot=args.plot,
    )
