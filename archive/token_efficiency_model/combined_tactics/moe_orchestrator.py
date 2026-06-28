from typing import Dict, List, Optional, Any, Tuple
import copy

from .rl_orchestrator import RLTokenOrchestrator, RLStep
from ..common.types import TacticConfig


class MoEOrchestrator:
    """Per-expert Q-learning. One RLTokenOrchestrator per expert id.

    Each expert's Q-table is warm-started from a shared 'operational' table
    on construction so cold-start episodes for new experts get a reasonable
    prior. Action selection respects per-expert action filters so each
    expert's Q-learning operates over a smaller, expert-relevant action
    subspace.
    """

    def __init__(self, expert_ids: List[str],
                 action_filters: Optional[Dict[str, List[int]]] = None):
        """Initialize MoE with one RLTokenOrchestrator per expert.

        Args:
            expert_ids: List of expert identifiers.
            action_filters: Optional dict mapping expert_id to list of allowed action indices.
                           If not provided, all actions are allowed for that expert.
        """
        # Build the seed table (will be cloned for each expert).
        seed = RLTokenOrchestrator()
        self.tables: Dict[str, RLTokenOrchestrator] = {}
        for eid in expert_ids:
            # Deep copy to share warm-start state but allow independent learning.
            cloned = copy.deepcopy(seed)
            self.tables[eid] = cloned
        # action_filters[eid] is a list of action indices the expert is allowed to choose.
        # Defaults to allowing every action when filter is missing.
        self.action_filters: Dict[str, List[int]] = action_filters or {}

    def select_action(self, expert_id: str, state: Tuple[int, ...],
                      *, explore: bool = True,
                      metrics: Optional[Dict[str, float]] = None) -> Tuple[int, TacticConfig]:
        """Delegate to the expert's RLTokenOrchestrator, but mask actions
        outside the expert's filter.

        Returns:
            Tuple of (action_idx, TacticConfig)
        """
        table = self.tables[expert_id]
        allowed = self.action_filters.get(expert_id)

        # Call select_action and get the action index
        action_idx, tactic = table.select_action(state, explore=explore, metrics=metrics)

        # If no filter, return as-is
        if allowed is None:
            return action_idx, tactic

        # If the selected action is already in the allowed set, return it
        if action_idx in allowed:
            return action_idx, tactic

        # Otherwise, pick the best allowed action by Q-value
        # KeyError when state isn't in the table yet (cold start).
        try:
            q_row = table.q_table[state]
            best_idx = max(allowed, key=lambda i: q_row[i] if i < len(q_row) else float('-inf'))
            return best_idx, table.actions[best_idx]
        except KeyError:
            fallback_idx = allowed[0] if allowed else action_idx
            return fallback_idx, table.actions[fallback_idx]

    def update(self, expert_id: str, step: RLStep) -> None:
        """Update the expert's Q-table with an RLStep."""
        self.tables[expert_id].update(step)

    def stats(self) -> Dict[str, Dict[str, Any]]:
        """Per-expert episode count + summary metrics, for benchmark reporting."""
        out = {}
        for eid, t in self.tables.items():
            pareto = t._compute_pareto_frontier() if hasattr(t, '_compute_pareto_frontier') else None
            out[eid] = {
                "state_visits": len(getattr(t, "state_visits", {})),
                "q_table_size": len(getattr(t, "q_table", {})),
                "best_pareto_frontier_size": len(pareto) if pareto else 0,
            }
        return out
