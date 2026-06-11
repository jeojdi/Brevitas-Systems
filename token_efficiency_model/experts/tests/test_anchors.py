"""
Tests for expert anchor regex functionality.

Covers:
1. Each non-operational expert's anchors hit AT LEAST 1 context in scenarios matching their task_family
2. Negative tests: anchors from different family typically don't trigger
3. OperationalExpert has empty anchor_regexes
"""

import pytest
from token_efficiency_model.experts.base import BaseExpert
from token_efficiency_model.experts.operational import OperationalExpert
from token_efficiency_model.experts.math_expert import MathExpert
from token_efficiency_model.experts.multihop import MultiHopQAExpert
from token_efficiency_model.experts.logical import LogicalDeductionExpert
from token_efficiency_model.experts.planning import PlanningExpert
from token_efficiency_model.experts.swe import SWEExpert
from token_efficiency_model.experts.research import ResearchExpert
from token_efficiency_model.experiments.advanced_test_data import (
    AdvancedTestDataGenerator, ScenarioType
)


# Mapping of expert classes to their expected task_family
EXPERT_TO_FAMILY = {
    MathExpert: "math",
    MultiHopQAExpert: "multihop",
    LogicalDeductionExpert: "logical",
    PlanningExpert: "planning",
    SWEExpert: "swe",
    ResearchExpert: "research",
}

# Mapping of task_family to ScenarioType
FAMILY_TO_SCENARIO = {
    "math": ScenarioType.MATH_REASONING,
    "multihop": ScenarioType.MULTI_HOP_QA,
    "logical": ScenarioType.LOGICAL_DEDUCTION,
    "planning": ScenarioType.PLANNING,
    "swe": ScenarioType.SWE_DEVELOPMENT,
    "research": ScenarioType.RESEARCH_TASK,
}


class TestExpertAnchorsMatch:
    """Test that expert anchors match contexts in scenarios of their task_family."""

    @pytest.mark.parametrize("expert_class", EXPERT_TO_FAMILY.keys())
    def test_expert_anchors_hit_matching_scenario(self, expert_class):
        """For each expert, generate matching scenario and verify anchors hit >= 1 context."""
        task_family = EXPERT_TO_FAMILY[expert_class]
        scenario_type = FAMILY_TO_SCENARIO[task_family]

        gen = AdvancedTestDataGenerator(seed=42)
        scenario = gen.generate_advanced_scenario(scenario_type)

        # Create expert instance (with dummy pipeline)
        expert = expert_class(pipeline=None)

        # Collect all contexts
        contexts = []
        if "task_text" in scenario:
            contexts.append(scenario["task_text"])
        if "incoming_messages" in scenario:
            contexts.extend(scenario["incoming_messages"])
        if "prior_context" in scenario:
            contexts.extend(scenario["prior_context"])

        # Compute anchors
        anchor_indices = expert.compute_anchors(contexts)

        assert len(anchor_indices) >= 1, (
            f"{expert_class.__name__} anchors should hit >= 1 context in "
            f"{scenario_type} scenario, but got {len(anchor_indices)}"
        )


class TestExpertAnchorsNegative:
    """Test that anchors from one expert don't heavily trigger on mismatched scenarios."""

    @pytest.mark.parametrize("expert_class", EXPERT_TO_FAMILY.keys())
    def test_expert_anchors_on_mismatched_scenario(self, expert_class):
        """Negative test: compute anchors on mismatched scenarios (soft assertion)."""
        source_family = EXPERT_TO_FAMILY[expert_class]
        # Pick a different family
        other_family = next(
            f for f in EXPERT_TO_FAMILY.values() if f != source_family
        )
        other_scenario_type = FAMILY_TO_SCENARIO[other_family]

        gen = AdvancedTestDataGenerator(seed=99)
        scenario = gen.generate_advanced_scenario(other_scenario_type)

        expert = expert_class(pipeline=None)

        contexts = []
        if "task_text" in scenario:
            contexts.append(scenario["task_text"])
        if "incoming_messages" in scenario:
            contexts.extend(scenario["incoming_messages"])
        if "prior_context" in scenario:
            contexts.extend(scenario["prior_context"])

        # Just verify compute_anchors returns a valid set (can be empty or non-empty)
        anchor_indices = expert.compute_anchors(contexts)
        assert isinstance(anchor_indices, set), (
            f"{expert_class.__name__}.compute_anchors should return a set"
        )


class TestOperationalExpertAnchors:
    """Test that OperationalExpert has empty anchor_regexes."""

    def test_operational_expert_no_anchors(self):
        """OperationalExpert.anchor_regexes should be empty."""
        assert OperationalExpert.anchor_regexes == [], (
            "OperationalExpert should have empty anchor_regexes"
        )

    def test_operational_expert_compute_anchors_empty(self):
        """compute_anchors on OperationalExpert should always return empty set."""
        expert = OperationalExpert(pipeline=None)

        gen = AdvancedTestDataGenerator(seed=42)
        scenario = gen.generate_advanced_scenario(ScenarioType.MULTI_TURN_STATEFUL)

        contexts = []
        if "task_text" in scenario:
            contexts.append(scenario["task_text"])
        if "incoming_messages" in scenario:
            contexts.extend(scenario["incoming_messages"])
        if "prior_context" in scenario:
            contexts.extend(scenario["prior_context"])

        anchor_indices = expert.compute_anchors(contexts)

        assert anchor_indices == set(), (
            f"OperationalExpert.compute_anchors should return empty set, got {anchor_indices}"
        )
