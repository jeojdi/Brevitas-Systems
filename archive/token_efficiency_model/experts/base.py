import re
from typing import List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from ..combined_tactics.pipeline import TokenEfficientPipeline
    from ..common.types import PipelineResult


class BaseExpert:
    """Base class for domain-specific experts that filter actions and inject anchors."""

    name: str = "base"
    anchor_regexes: List[re.Pattern] = []

    @staticmethod
    def excluded_action_predicate(cfg) -> bool:
        """Override in subclasses to filter actions.

        Args:
            cfg: A TacticConfig or similar action config object

        Returns:
            True if the action should be EXCLUDED, False if ALLOWED
        """
        return False

    def __init__(self, pipeline: "TokenEfficientPipeline"):
        """Initialize expert with a pipeline reference.

        Args:
            pipeline: The TokenEfficientPipeline instance to wrap
        """
        self.pipeline = pipeline

    def allowed_actions(self, all_actions: List) -> List[int]:
        """Filter actions based on the expert's predicate.

        Args:
            all_actions: List of action config objects (TacticConfig)

        Returns:
            Indices of actions that pass the expert's filter (not excluded)
        """
        return [
            i for i, cfg in enumerate(all_actions)
            if not type(self).excluded_action_predicate(cfg)
        ]

    def compute_anchors(self, contexts: List[str]) -> Set[int]:
        """Compute indices of contexts matching any anchor regex.

        Args:
            contexts: List of context strings to search

        Returns:
            Set of indices where anchor_regexes match
        """
        idx: Set[int] = set()
        for r in type(self).anchor_regexes:
            for i, c in enumerate(contexts):
                if r.search(c):
                    idx.add(i)
        return idx

    def run_via_process(
        self,
        *,
        must_keep_facts: Optional[List[str]] = None,
        **kwargs
    ) -> "PipelineResult":
        """Run the pipeline with optional anchor injection.

        If anchor_regexes is empty, calls pipeline.process_task directly.
        Otherwise, monkeypatches the sampler's _anchor_indices method for
        the duration of the call to inject regex-matched indices. Restores
        the original method in a finally block.

        Args:
            must_keep_facts: Optional list of facts that must be preserved
            **kwargs: Passed to pipeline.process_task (task_text, incoming_messages, etc.)

        Returns:
            PipelineResult from the pipeline
        """
        # If no anchor regexes, pass through directly
        if not type(self).anchor_regexes:
            return self.pipeline.process_task(
                must_keep_facts=must_keep_facts,
                **kwargs
            )

        # Monkeypatch the sampler's _anchor_indices method
        sampler = self.pipeline.semantic_sampler
        original_anchor_indices = sampler._anchor_indices

        try:
            # Create a wrapper that unions default anchors with regex-based ones
            def wrapped_anchor_indices(*args, **kw):
                # Call original to get default anchors
                default_anchors = original_anchor_indices(*args, **kw)

                # Extract contexts from the first positional arg or kwargs
                # Signature: _anchor_indices(self, contexts, scored)
                if args:
                    contexts = args[0]
                elif 'contexts' in kw:
                    contexts = kw['contexts']
                else:
                    # Fallback: no contexts available
                    return default_anchors

                # Compute regex-based anchors
                regex_anchors = self.compute_anchors(contexts)

                # Return union
                return default_anchors.union(regex_anchors)

            # Replace the method
            sampler._anchor_indices = wrapped_anchor_indices

            # Run the pipeline
            return self.pipeline.process_task(
                must_keep_facts=must_keep_facts,
                **kwargs
            )
        finally:
            # Always restore the original method
            sampler._anchor_indices = original_anchor_indices
