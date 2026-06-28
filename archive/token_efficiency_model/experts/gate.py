"""
Expert routing gate for task distribution.

Routes scenarios to one of 7 expert domains:
- operational, math, multihop, logical, planning, swe, research
"""

import re
from typing import Dict, Any, Union


# Task family to expert mapping
TASK_FAMILY_TO_EXPERT = {
    "operational": "operational",
    "math": "math",
    "multihop": "multihop",
    "logical": "logical",
    "planning": "planning",
    "swe": "swe",
    "research": "research",
}

# Scenario type to expert mapping (14 scenario types)
SCENARIO_TYPE_TO_EXPERT = {
    "multi_turn_stateful": "operational",
    "high_complexity_reasoning": "operational",
    "domain_specific": "operational",
    "cross_team_communication": "operational",
    "timeseries_analysis": "operational",
    "adversarial_pruning": "operational",
    "cascading_decisions": "operational",
    "emergent_behavior": "operational",
    "math_reasoning": "math",
    "multi_hop_qa": "multihop",
    "logical_deduction": "logical",
    "planning": "planning",
    "swe_development": "swe",
    "research_task": "research",
}


def _extract_text(scenario_or_packet) -> str:
    """
    Extract combined text from scenario dict or TaskPacket.
    Handles both dict and object (with attributes).
    """
    parts = []

    # Try dict access first
    if isinstance(scenario_or_packet, dict):
        task_text = scenario_or_packet.get("task_text", "")
        if task_text:
            parts.append(task_text)

        incoming_messages = scenario_or_packet.get("incoming_messages", [])
        if incoming_messages:
            if isinstance(incoming_messages, list):
                parts.extend(incoming_messages)
            else:
                parts.append(str(incoming_messages))

        prior_context = scenario_or_packet.get("prior_context", [])
        if prior_context:
            if isinstance(prior_context, list):
                parts.extend(prior_context)
            else:
                parts.append(str(prior_context))
    else:
        # Try object attributes
        task_text = getattr(scenario_or_packet, "task_text", None)
        if task_text:
            parts.append(task_text)

        incoming_messages = getattr(scenario_or_packet, "incoming_messages", None)
        if incoming_messages:
            if isinstance(incoming_messages, list):
                parts.extend(incoming_messages)
            else:
                parts.append(str(incoming_messages))

        prior_context = getattr(scenario_or_packet, "prior_context", None)
        if prior_context:
            if isinstance(prior_context, list):
                parts.extend(prior_context)
            else:
                parts.append(str(prior_context))

    return " ".join(str(p) for p in parts)


def _extract_features(text: str) -> Dict[str, float]:
    """
    Extract 6 features from joined text.
    Returns dict with keys: numeric_density, capitalized_rate, implication_rate,
    sequence_marker_rate, code_density, citation_density.
    """
    tokens = text.split()
    token_count = len(tokens)

    # Handle empty text
    if token_count == 0:
        return {
            "numeric_density": 0.0,
            "capitalized_rate": 0.0,
            "implication_rate": 0.0,
            "sequence_marker_rate": 0.0,
            "code_density": 0.0,
            "citation_density": 0.0,
        }

    # 1. numeric_density: fraction of tokens matching \d+
    numeric_tokens = sum(1 for t in tokens if re.match(r'^\d+', t))
    numeric_density = numeric_tokens / token_count

    # 2. capitalized_rate: fraction of tokens starting with capital letter
    capitalized_tokens = sum(1 for t in tokens if t and t[0].isupper())
    capitalized_rate = capitalized_tokens / token_count

    # 3. implication_rate: count of {if,then,therefore,implies,because,hence,thus} per 100 tokens
    implication_words = {"if", "then", "therefore", "implies", "because", "hence", "thus"}
    implication_count = sum(1 for t in tokens if t.lower() in implication_words)
    implication_rate = (implication_count / token_count) * 100 if token_count > 0 else 0.0

    # 4. sequence_marker_rate: count of {first,then,finally,next,step,precondition} per 100 tokens
    sequence_words = {"first", "then", "finally", "next", "step", "precondition"}
    sequence_count = sum(1 for t in tokens if t.lower() in sequence_words)
    sequence_marker_rate = (sequence_count / token_count) * 100 if token_count > 0 else 0.0

    # 5. code_density: count of backtick spans + def/class/import + file:line patterns per 100 tokens
    backtick_pairs = len(re.findall(r'`[^`]+`', text))
    def_count = text.lower().count("def ")
    class_count = text.lower().count("class ")
    import_count = text.lower().count("import ")
    file_line_count = len(re.findall(r'\w+\.\w+:\d+', text))
    code_markers = backtick_pairs + def_count + class_count + import_count + file_line_count
    code_density = (code_markers / token_count) * 100 if token_count > 0 else 0.0

    # 6. citation_density: count of (Author, YEAR) patterns + p< + n= per 100 tokens
    citation_patterns = len(re.findall(r'\([A-Z][a-z]+,\s*\d{4}\)', text))
    p_angle_count = text.count("p<")
    n_equal_count = text.count("n=")
    citation_markers = citation_patterns + p_angle_count + n_equal_count
    citation_density = (citation_markers / token_count) * 100 if token_count > 0 else 0.0

    return {
        "numeric_density": numeric_density,
        "capitalized_rate": capitalized_rate,
        "implication_rate": implication_rate,
        "sequence_marker_rate": sequence_marker_rate,
        "code_density": code_density,
        "citation_density": citation_density,
    }


class RuleGate:
    """
    Deterministic routing gate using rules and heuristics.

    Priority order:
    1. Explicit task_family (if present and valid)
    2. scenario_type mapping (14 types)
    3. Feature-based heuristic (argmax of 6 features)
    """

    def route(self, scenario_or_packet) -> str:
        """
        Route scenario/packet to expert.
        Returns one of: operational, math, multihop, logical, planning, swe, research
        """
        # Priority 1: Check explicit task_family
        task_family = None
        if isinstance(scenario_or_packet, dict):
            task_family = scenario_or_packet.get("task_family")
        else:
            task_family = getattr(scenario_or_packet, "task_family", None)

        if task_family and task_family in TASK_FAMILY_TO_EXPERT:
            return TASK_FAMILY_TO_EXPERT[task_family]

        # Priority 2: Check scenario_type
        scenario_type = None
        if isinstance(scenario_or_packet, dict):
            scenario_type = scenario_or_packet.get("scenario_type")
        else:
            scenario_type = getattr(scenario_or_packet, "scenario_type", None)

        # Handle enum values (convert to string if needed)
        if scenario_type is not None:
            scenario_type_str = str(scenario_type).lower()
            # Try exact match first
            if scenario_type_str in SCENARIO_TYPE_TO_EXPERT:
                return SCENARIO_TYPE_TO_EXPERT[scenario_type_str]
            # Try enum value (e.g., "ScenarioType.MATH_REASONING" -> "math_reasoning")
            if "." in scenario_type_str:
                enum_value = scenario_type_str.split(".")[-1].lower()
                if enum_value in SCENARIO_TYPE_TO_EXPERT:
                    return SCENARIO_TYPE_TO_EXPERT[enum_value]

        # Priority 3: Feature-based heuristic
        text = _extract_text(scenario_or_packet)
        features = _extract_features(text)

        # Map features to experts
        expert_scores = {
            "math": features["numeric_density"],
            "multihop": features["capitalized_rate"],
            "logical": features["implication_rate"],
            "planning": features["sequence_marker_rate"],
            "swe": features["code_density"],
            "research": features["citation_density"],
        }

        # Find argmax expert
        max_score = max(expert_scores.values())

        # If all scores are below threshold, default to operational
        if max_score < 0.01:
            return "operational"

        # Return expert with highest score
        for expert, score in expert_scores.items():
            if score == max_score:
                return expert

        # Fallback (should not reach here)
        return "operational"


class LearnedGate:
    """
    Learned routing gate using scikit-learn LogisticRegression.
    Falls back to RuleGate if sklearn unavailable or not fitted.
    """

    def __init__(self, rule_fallback: RuleGate = None):
        """
        Initialize LearnedGate.

        Args:
            rule_fallback: RuleGate to use if learned model not fitted
        """
        self.rule_fallback = rule_fallback or RuleGate()
        self._fitted = False
        self._model = None
        self._experts = ["operational", "math", "multihop", "logical", "planning", "swe", "research"]
        self._expert_to_idx = {e: i for i, e in enumerate(self._experts)}

    def fit_from(self, generator, n_samples: int = 500):
        """
        Fit logistic regression on samples from generator.

        Args:
            generator: AdvancedTestDataGenerator instance
            n_samples: Number of samples to generate and fit
        """
        try:
            from sklearn.linear_model import LogisticRegression
        except ImportError:
            self._fitted = False
            return

        X = []
        y = []

        # Generate samples and extract features
        workload = generator.generate_workload(num_scenarios=n_samples)
        for scenario in workload:
            # Extract features
            text = _extract_text(scenario)
            features = _extract_features(text)
            feature_vector = [
                features["numeric_density"],
                features["capitalized_rate"],
                features["implication_rate"],
                features["sequence_marker_rate"],
                features["code_density"],
                features["citation_density"],
            ]
            X.append(feature_vector)

            # Get label from RuleGate
            expert = self.rule_fallback.route(scenario)
            y.append(self._expert_to_idx[expert])

        # Fit model
        self._model = LogisticRegression(max_iter=200, random_state=42)
        self._model.fit(X, y)
        self._fitted = True

    def route(self, scenario_or_packet) -> str:
        """
        Route using learned model, fallback to rule if not fitted.
        """
        if not self._fitted or self._model is None:
            return self.rule_fallback.route(scenario_or_packet)

        # Extract features
        text = _extract_text(scenario_or_packet)
        features = _extract_features(text)
        feature_vector = [
            features["numeric_density"],
            features["capitalized_rate"],
            features["implication_rate"],
            features["sequence_marker_rate"],
            features["code_density"],
            features["citation_density"],
        ]

        # Predict
        predicted_idx = self._model.predict([feature_vector])[0]
        return self._experts[predicted_idx]

    def agreement_rate(self, generator, n_samples: int = 500) -> float:
        """
        Compare learned model predictions to RuleGate labels on held-out samples.

        Args:
            generator: AdvancedTestDataGenerator instance
            n_samples: Number of samples to evaluate

        Returns:
            Float between 0 and 1 representing fraction of agreements
        """
        if not self._fitted or self._model is None:
            return 0.0

        agreements = 0
        workload = generator.generate_workload(num_scenarios=n_samples)
        for scenario in workload:
            # Compare predictions
            learned_prediction = self.route(scenario)
            rule_prediction = self.rule_fallback.route(scenario)

            if learned_prediction == rule_prediction:
                agreements += 1

        return agreements / n_samples if n_samples > 0 else 0.0


class Gate:
    """
    Public façade for expert routing.
    Supports both rule-based and learned modes.
    """

    def __init__(self, mode: str = "rule"):
        """
        Initialize Gate.

        Args:
            mode: "rule" for deterministic routing, "learned" for ML-based routing
        """
        self.mode = mode
        self._rule = RuleGate()
        self._learned = LearnedGate(self._rule) if mode == "learned" else None

    def route(self, scenario_or_packet) -> str:
        """
        Route scenario/packet to expert.

        Args:
            scenario_or_packet: Dict or TaskPacket with scenario data

        Returns:
            Expert name: one of {operational, math, multihop, logical, planning, swe, research}
        """
        if self.mode == "learned" and self._learned is not None and self._learned._fitted:
            return self._learned.route(scenario_or_packet)
        return self._rule.route(scenario_or_packet)

    def fit(self, generator, n_samples: int = 500):
        """
        Fit the learned model (no-op if mode is "rule").

        Args:
            generator: AdvancedTestDataGenerator instance
            n_samples: Number of samples to train on
        """
        if self._learned is not None:
            self._learned.fit_from(generator, n_samples=n_samples)
