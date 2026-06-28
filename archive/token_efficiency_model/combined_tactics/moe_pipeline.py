from typing import Any, Dict, List, Optional
from .pipeline import TokenEfficientPipeline, PipelineResult
from ..experts.gate import Gate
from ..experts.operational import OperationalExpert
from ..experts.math_expert import MathExpert
from ..experts.multihop import MultiHopQAExpert
from ..experts.logical import LogicalDeductionExpert
from ..experts.planning import PlanningExpert
from ..experts.swe import SWEExpert
from ..experts.research import ResearchExpert
from ..common.recall import critical_context_recall


class MoEPipeline:
    """Mixture-of-Experts wrapper around TokenEfficientPipeline.

    Routes each task to one of 7 specialized experts (via Gate), runs the
    underlying pipeline with that expert's anchor injection + action filter,
    then computes critical-context recall as the quality metric when
    must_keep_facts is provided.
    """

    def __init__(self, model_backend=None, memory_persistence_path: str = "",
                 quality_floor: float = 0.98,
                 savings_target: float = 70.0,
                 max_tuning_attempts: int = 3):
        self.shared = TokenEfficientPipeline(
            model_backend=model_backend,
            memory_persistence_path=memory_persistence_path,
            quality_floor=quality_floor,
            savings_target=savings_target,
            max_tuning_attempts=max_tuning_attempts,
        )
        self.experts = {
            "operational": OperationalExpert(self.shared),
            "math": MathExpert(self.shared),
            "multihop": MultiHopQAExpert(self.shared),
            "logical": LogicalDeductionExpert(self.shared),
            "planning": PlanningExpert(self.shared),
            "swe": SWEExpert(self.shared),
            "research": ResearchExpert(self.shared),
        }
        self.gate = Gate(mode="rule")

    def process_task(self, *,
                     must_keep_facts: Optional[List[str]] = None,
                     task_family: Optional[str] = None,
                     scenario_type: Optional[str] = None,
                     **kwargs) -> PipelineResult:
        # 1. Route to expert
        routing_input = {
            "task_family": task_family,
            "scenario_type": scenario_type,
            "task_text": kwargs.get("task_text", ""),
            "prior_context": kwargs.get("prior_context", []),
            "incoming_messages": kwargs.get("incoming_messages", []),
        }
        expert_id = self.gate.route(routing_input)
        if expert_id not in self.experts:
            expert_id = "operational"  # safety net

        # 2. Run via expert (with anchor injection + action filter)
        result = self.experts[expert_id].run_via_process(
            must_keep_facts=must_keep_facts, **kwargs
        )

        # 3. Compute recall when must_keep_facts is supplied
        if must_keep_facts:
            surviving = []
            surviving.extend(kwargs.get("prior_context", []))
            surviving.extend(kwargs.get("incoming_messages", []))
            if kwargs.get("task_text"):
                surviving.append(kwargs["task_text"])
            surviving.extend(result.debug.get("pruned_context", []))
            surviving.extend(result.debug.get("compressed_messages", []))
            surviving.extend(result.debug.get("inline_chunks", []))
            surviving_text = " ".join(s for s in surviving if isinstance(s, str))
            recall = critical_context_recall(must_keep_facts, surviving_text)
            # Overwrite quality_proxy with recall — semantically still in [0,1]
            result.quality_proxy = recall
            result.debug["must_keep_recall"] = recall

        # 4. Stamp routing metadata onto debug
        result.debug["expert_id"] = expert_id
        return result

    def expert_action_filter(self, expert_id: str, all_actions: List[Any]) -> List[int]:
        """Return indices of actions in all_actions that the expert allows.
        Used by MoEOrchestrator to constrain Q-learning per expert.
        """
        expert = self.experts.get(expert_id)
        if expert is None:
            return list(range(len(all_actions)))
        return expert.allowed_actions(all_actions)
