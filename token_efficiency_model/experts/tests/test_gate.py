"""
Tests for expert routing gate functionality.

Covers:
1. RuleGate.route agrees with task_family on every ScenarioType
2. task_family overrides scenario_type
3. scenario_type mapping for all 14 enum values when task_family absent
4. Heuristic fallback responds to features
5. LearnedGate agreement rate >= 0.85
6. Gate façade in mode='rule' returns RuleGate's answer for all ScenarioTypes
"""

import pytest
from token_efficiency_model.experts.gate import (
    RuleGate, LearnedGate, Gate, TASK_FAMILY_TO_EXPERT, SCENARIO_TYPE_TO_EXPERT
)
from token_efficiency_model.experiments.advanced_test_data import (
    AdvancedTestDataGenerator, ScenarioType
)


class TestRuleGateAgreement:
    """Test 1: RuleGate.route agrees with task_family on every ScenarioType."""

    @pytest.mark.parametrize("scenario_type", list(ScenarioType))
    def test_route_agrees_with_task_family(self, scenario_type):
        """Generate one scenario per ScenarioType; assert gate returns task_family."""
        gen = AdvancedTestDataGenerator(seed=42)
        scenario = gen.generate_advanced_scenario(scenario_type)

        gate = RuleGate()
        result = gate.route(scenario)

        # Scenario should have task_family set
        assert "task_family" in scenario
        expected = scenario["task_family"]
        assert result == expected, (
            f"Gate returned {result} but scenario task_family is {expected} "
            f"for {scenario_type}"
        )


class TestTaskFamilyOverride:
    """Test 2: task_family overrides scenario_type."""

    def test_task_family_overrides_scenario_type(self):
        """Build dict with task_family='math' but scenario_type='multi_turn_stateful'."""
        scenario = {
            "task_family": "math",
            "scenario_type": "multi_turn_stateful",
            "task_text": "Some text",
        }

        gate = RuleGate()
        result = gate.route(scenario)

        # Should return 'math' because task_family takes priority
        assert result == "math"


class TestScenarioTypeMapping:
    """Test 3: scenario_type mapping for all 14 enum values when task_family absent."""

    @pytest.mark.parametrize("scenario_type", list(ScenarioType))
    def test_scenario_type_routing_without_task_family(self, scenario_type):
        """Test that each ScenarioType maps to expected expert via SCENARIO_TYPE_TO_EXPERT."""
        # Build a dict with scenario_type but NO task_family
        scenario = {
            "scenario_type": scenario_type.value,  # Use .value to get string
            "task_text": "Some text",
            "incoming_messages": [],
            "prior_context": [],
        }

        gate = RuleGate()
        result = gate.route(scenario)

        # Get expected expert from mapping
        expected = SCENARIO_TYPE_TO_EXPERT.get(scenario_type.value)

        assert expected is not None, f"No mapping for {scenario_type.value}"
        assert result == expected, (
            f"Gate returned {result} but expected {expected} for {scenario_type}"
        )


class TestHeuristicFallback:
    """Test 4: Heuristic fallback responds to features."""

    def test_heuristic_routes_to_math(self):
        """Input with high numeric density should route to math."""
        scenario = {
            "task_text": "Calculate 123 + 456 = 579. Compute 789 / 3 = 263. Multiply 100 * 50 = 5000.",
            "incoming_messages": ["100 200 300 400 500 600 700 800"],
            "prior_context": ["1 2 3 4 5 6 7 8 9 10"],
        }

        gate = RuleGate()
        result = gate.route(scenario)

        assert result == "math", f"Expected math, got {result}"

    def test_heuristic_routes_to_swe(self):
        """Input with high code density should route to swe."""
        scenario = {
            "task_text": "Fix bug in file.py:42. def process_data(x): return x.",
            "incoming_messages": [
                "Error in handlers.py:100 from module.errors import ValueError",
                "Add tests/test_handlers.py with coverage",
            ],
            "prior_context": ["class DataProcessor: def __init__(self): pass"],
        }

        gate = RuleGate()
        result = gate.route(scenario)

        assert result == "swe", f"Expected swe, got {result}"

    def test_heuristic_routes_to_research(self):
        """Input with high citation density should route to research."""
        scenario = {
            "task_text": "Synthesize findings. (Smith, 2020) and (Jones, 2021) report p<0.05.",
            "incoming_messages": [
                "(Brown, 2019) found n=1000 samples, p=0.001, 25% improvement",
            ],
            "prior_context": ["Meta-analysis: p<0.01, n=5000"],
        }

        gate = RuleGate()
        result = gate.route(scenario)

        assert result == "research", f"Expected research, got {result}"

    def test_heuristic_routes_to_planning(self):
        """Input with high sequence marker rate should route to planning."""
        scenario = {
            "task_text": "Create a plan. first do X. then do Y. next do Z. finally do W.",
            "incoming_messages": [
                "Step 1: review code. Step 2: test. Step 3: deploy. precondition: approval",
            ],
            "prior_context": ["Process: first plan, then execute, next monitor, finally rollback"],
        }

        gate = RuleGate()
        result = gate.route(scenario)

        assert result == "planning", f"Expected planning, got {result}"


class TestLearnedGateAgreement:
    """Test 5: LearnedGate agreement rate >= 0.85."""

    def test_learned_gate_agreement_rate(self):
        """Fit on 200 samples; measure on 100 held-out samples; assert >= 0.85."""
        try:
            from sklearn.linear_model import LogisticRegression
        except ImportError:
            pytest.skip("sklearn not available")

        gen = AdvancedTestDataGenerator(seed=42)

        gate = LearnedGate()
        gate.fit_from(gen, n_samples=200)

        assert gate._fitted, "LearnedGate did not fit"

        # Measure agreement on held-out samples
        agreement = gate.agreement_rate(gen, n_samples=100)

        assert agreement >= 0.85, (
            f"LearnedGate agreement rate {agreement:.3f} < 0.85 threshold"
        )

    def test_learned_gate_sklearn_unavailable_skip(self):
        """If sklearn missing, fit_from should skip fitting and test should skip."""
        # Mock sklearn import failure
        import sys
        orig_modules = sys.modules.copy()
        if 'sklearn' in sys.modules:
            del sys.modules['sklearn']
        if 'sklearn.linear_model' in sys.modules:
            del sys.modules['sklearn.linear_model']

        # Block sklearn import
        sys.modules['sklearn'] = None

        try:
            gen = AdvancedTestDataGenerator(seed=42)
            gate = LearnedGate()
            gate.fit_from(gen, n_samples=10)

            # If sklearn is unavailable, _fitted should be False
            if not gate._fitted:
                pytest.skip("sklearn unavailable, LearnedGate not fitted")
        finally:
            sys.modules.update(orig_modules)


class TestGateFacade:
    """Test 6: Gate façade in mode='rule' returns RuleGate's answer."""

    @pytest.mark.parametrize("scenario_type", list(ScenarioType))
    def test_gate_rule_mode_matches_rulegat(self, scenario_type):
        """Gate in rule mode should match RuleGate for all ScenarioTypes."""
        gen = AdvancedTestDataGenerator(seed=42)
        scenario = gen.generate_advanced_scenario(scenario_type)

        rule_gate = RuleGate()
        gate = Gate(mode="rule")

        rule_result = rule_gate.route(scenario)
        gate_result = gate.route(scenario)

        assert gate_result == rule_result, (
            f"Gate(rule) returned {gate_result} but RuleGate returned {rule_result} "
            f"for {scenario_type}"
        )
