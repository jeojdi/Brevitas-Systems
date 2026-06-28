import random
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from ..common.types import TacticConfig


@dataclass
class RLStep:
    state: Tuple[int, ...]
    action_idx: int
    reward: float
    next_state: Tuple[int, ...]


class RLTokenOrchestrator:
    def __init__(self, alpha: float = 0.15, gamma: float = 0.92, epsilon: float = 0.12):
        self.alpha = alpha
        self.gamma = gamma
        self.epsilon = epsilon
        self.actions = self._build_actions()
        self.q_table: Dict[Tuple[int, ...], np.ndarray] = {}
        self.state_visits: Dict[Tuple[int, ...], int] = {}
        self.action_visits: Dict[Tuple[int, int], int] = {}
        self.last_selected_reason: str = "q_max"

    def _build_actions(self) -> List[TacticConfig]:
        actions = []
        for compression_level in [1, 2, 3]:
            for prune_budget in [3, 5, 8]:
                for protocol_mode in ["compact", "raw-json"]:
                    for delta_mode in ["off", "state-delta"]:
                        for delta_aggressiveness in [1, 2, 3]:
                            for wire_mode in ["json", "binary"]:
                                actions.append(
                                    TacticConfig(
                                        compression_level=compression_level,
                                        prune_budget=prune_budget,
                                        protocol_mode=protocol_mode,
                                        use_shared_memory=True,
                                        delta_mode=delta_mode,
                                        delta_aggressiveness=delta_aggressiveness,
                                        wire_mode=wire_mode,
                                    )
                                )
        return actions

    def discretize_state(
        self,
        complexity: float,
        urgency: float,
        context_load: float,
        cache_hit_rate: float = 0.0,
        continuity: float = 0.0,
    ) -> Tuple[int, int, int, int, int]:
        c_bin = min(2, int(complexity * 3))
        u_bin = min(2, int(urgency * 3))
        l_bin = min(2, int(context_load * 3))
        h_bin = min(2, int(cache_hit_rate * 3))
        n_bin = min(2, int(continuity * 3))
        return c_bin, u_bin, l_bin, h_bin, n_bin

    def _ensure_state(self, state: Tuple[int, ...]) -> None:
        if state not in self.q_table:
            self.q_table[state] = np.zeros(len(self.actions), dtype=float)
        if state not in self.state_visits:
            self.state_visits[state] = 0

    def _action_profile(self, action: TacticConfig) -> Tuple[float, float]:
        expected_savings = 0.0
        expected_savings += (action.compression_level - 1) / 2.0
        expected_savings += max(0.0, (8 - action.prune_budget) / 5.0)
        expected_savings += 0.25 if action.protocol_mode == "compact" else 0.0
        expected_savings += 0.35 if action.delta_mode == "state-delta" else 0.0
        expected_savings += 0.15 if action.wire_mode == "binary" else 0.0
        expected_savings += 0.10 * (action.delta_aggressiveness - 1)

        quality_risk = 0.0
        quality_risk += 0.30 * ((action.compression_level - 1) / 2.0)
        quality_risk += 0.35 * max(0.0, (5 - action.prune_budget) / 4.0)
        quality_risk += 0.20 if action.protocol_mode == "compact" else 0.0
        quality_risk += 0.18 if action.delta_mode == "state-delta" else 0.0
        quality_risk += 0.08 if action.delta_aggressiveness >= 3 else 0.0

        expected_savings = max(0.0, min(1.0, expected_savings / 2.2))
        expected_quality = max(0.0, min(1.0, 1.0 - min(1.0, quality_risk)))
        return expected_savings, expected_quality

    def _compute_pareto_frontier(self) -> List[int]:
        frontier: List[int] = []
        profiles = [self._action_profile(action) for action in self.actions]

        for idx, (savings_i, quality_i) in enumerate(profiles):
            dominated = False
            for jdx, (savings_j, quality_j) in enumerate(profiles):
                if jdx == idx:
                    continue
                if (
                    savings_j >= savings_i
                    and quality_j >= quality_i
                    and (savings_j > savings_i or quality_j > quality_i)
                ):
                    dominated = True
                    break
            if not dominated:
                frontier.append(idx)
        return frontier

    def _best_frontier_action(self, state: Tuple[int, ...]) -> int:
        frontier = self._compute_pareto_frontier()
        if not frontier:
            return int(np.argmax(self.q_table[state]))

        best_idx = frontier[0]
        best_score = float("-inf")
        state_count = max(1, self.state_visits.get(state, 0))

        for action_idx in frontier:
            q_value = float(self.q_table[state][action_idx])
            action_count = self.action_visits.get((state[0], action_idx), 0)
            exploration_bonus = math.sqrt(math.log(state_count + 1.0) / (action_count + 1.0))
            frontier_score = q_value + 0.05 * exploration_bonus
            if frontier_score > best_score:
                best_score = frontier_score
                best_idx = action_idx

        return best_idx

    def select_action(
        self,
        state: Tuple[int, ...],
        explore: bool = True,
        metrics: Dict[str, float] = None,
    ) -> Tuple[int, TacticConfig]:
        self._ensure_state(state)
        self.state_visits[state] += 1

        if explore and random.random() < self.epsilon:
            action_idx = random.randrange(len(self.actions))
            self.last_selected_reason = "epsilon_explore"
            self.action_visits[(state[0], action_idx)] = self.action_visits.get((state[0], action_idx), 0) + 1
            return action_idx, self.actions[action_idx]

        prefer_pareto = True
        if metrics:
            quality = metrics.get("quality", 1.0)
            savings = metrics.get("savings", 0.0)
            prefer_pareto = quality < 0.995 or savings < 50.0

        if prefer_pareto:
            action_idx = self._best_frontier_action(state)
            self.last_selected_reason = "pareto_frontier"
        else:
            q_values = self.q_table[state]
            action_idx = int(np.argmax(q_values))
            self.last_selected_reason = "q_max"

        self.action_visits[(state[0], action_idx)] = self.action_visits.get((state[0], action_idx), 0) + 1
        return action_idx, self.actions[action_idx]

    def update(self, step: RLStep) -> None:
        self._ensure_state(step.state)
        self._ensure_state(step.next_state)

        current_q = self.q_table[step.state][step.action_idx]
        max_next = float(np.max(self.q_table[step.next_state]))
        updated = current_q + self.alpha * (step.reward + self.gamma * max_next - current_q)
        self.q_table[step.state][step.action_idx] = updated
