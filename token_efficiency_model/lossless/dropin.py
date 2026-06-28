"""Drop-in middleware wrapper for lossless token savings across OpenAI/Anthropic/DeepSeek.

USAGE (3-line):
  from token_efficiency_model.lossless.dropin import BrevitasDropIn
  client = BrevitasDropIn(base_url="https://api.openai.com/v1", provider="openai", api_key="...")
  response, savings = client.chat(messages=[...], model="gpt-4", ...)

The wrapper:
1. Applies provider-native prefix caching (Anthropic: breakpoints; OpenAI/DeepSeek: stable prefix)
2. Optionally reduces oversized context via retrieval (fail-safe to full)
3. Returns the model response + honest savings report
4. Detects provider from model name or explicit arg; routes base_url automatically

No breaking changes to the underlying provider API — pass any chat() args through.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from .api_adapter import retrieval_select
from .provider_cache import apply_anthropic_cache, savings_from_usage


@dataclass
class SavingsReport:
    """Savings from a single chat call."""
    provider: str
    cached_tokens: int
    uncached_cost: float
    actual_cost: float
    savings_pct: float
    cache_placement: Optional[Dict[str, Any]] = None  # Anthropic: CachePlan details
    retrieval_applied: bool = False
    retrieval_baseline_tokens: Optional[int] = None
    retrieval_optimized_tokens: Optional[int] = None


class BrevitasDropIn:
    """Drop-in middleware for OpenAI/Anthropic/DeepSeek chat APIs.

    Wraps a base_url + api_key and applies lossless token savings:
    - Provider-native caching (Anthropic breakpoints, OpenAI/DeepSeek prefix cache)
    - Optional retrieval-based context reduction (fail-safe to full context)
    - Honest savings computation from real usage
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """Initialize the drop-in wrapper.

        Args:
            base_url: The API endpoint (auto-detected from provider if not given).
            provider: "anthropic", "openai", "deepseek", or None (auto-detect from model).
            api_key: API key for the provider.
        """
        self.base_url = base_url
        self.provider = provider
        self.api_key = api_key
        self._client = None

    def _detect_provider(self, model: Optional[str] = None) -> str:
        """Detect provider from explicit arg, model name, or base_url."""
        if self.provider:
            return self.provider.lower()
        if model:
            model_lower = model.lower()
            if "claude" in model_lower:
                return "anthropic"
            if "gpt" in model_lower or "text-" in model_lower:
                return "openai"
            if "deepseek" in model_lower:
                return "deepseek"
        if "anthropic" in self.base_url.lower():
            return "anthropic"
        return "openai"  # default fallback

    def _route_client(self, provider: str) -> Any:
        """Return or create the appropriate client for the provider."""
        if self._client is not None and hasattr(self._client, "__provider__"):
            return self._client

        if provider == "anthropic":
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self.api_key)
                client.__provider__ = "anthropic"
                self._client = client
                return client
            except ImportError:
                raise ImportError(
                    "anthropic package required; install with: pip install anthropic"
                )
        else:
            # OpenAI or DeepSeek style (compatible API)
            try:
                import openai
                client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
                client.__provider__ = provider
                self._client = client
                return client
            except ImportError:
                raise ImportError(
                    "openai package required; install with: pip install openai"
                )

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: str,
        use_retrieval: bool = False,
        retrieval_k: int = 5,
        retrieval_min_score: float = 0.2,
        **kwargs: Any,
    ) -> Tuple[Any, SavingsReport]:
        """Call the provider's chat API with lossless token savings applied.

        Args:
            messages: Chat messages (standard OpenAI/Anthropic format).
            model: Model name (used for provider detection if needed).
            use_retrieval: If True, apply retrieval-based context reduction (fail-safe to full).
            retrieval_k: Top-k chunks to retrieve (if use_retrieval=True).
            retrieval_min_score: Min confidence to accept retrieved context.
            **kwargs: All other chat() args (temperature, max_tokens, tools, etc.).

        Returns:
            (response, savings_report) where response is the model's chat.completion,
            and savings_report is a SavingsReport with honest cost/token metrics.
        """
        provider = self._detect_provider(model)
        client = self._route_client(provider)

        # Prepare the request body
        body = {"messages": messages, "model": model, **kwargs}

        # Optional: apply retrieval-based context reduction
        retrieval_meta = None
        if use_retrieval and messages:
            prior_context = self._extract_context_chunks(messages)
            if prior_context:
                retrieval_meta = retrieval_select(
                    task=self._extract_task(messages),
                    prior_context=prior_context,
                    k=retrieval_k,
                    min_top_score=retrieval_min_score,
                )
                if retrieval_meta.get("selected_context") and not retrieval_meta.get(
                    "fallback_applied"
                ):
                    # Safe to reduce context; rebuild messages with selected chunks
                    messages = self._rebuild_messages_with_retrieved(
                        messages, retrieval_meta["selected_context"], prior_context
                    )
                    body["messages"] = messages

        # Apply provider-specific caching/optimization
        cache_plan = None
        if provider == "anthropic":
            cache_plan = apply_anthropic_cache(body)
            # Convert body to Anthropic's format if needed
            # (apply_anthropic_cache modifies body in-place with cache_control markers)
        else:
            # OpenAI/DeepSeek: rely on their automatic prefix caching
            # (just don't mutate the stable prefix; it's handled server-side)
            pass

        # Call the provider
        if provider == "anthropic":
            response = client.messages.create(**body)
        else:
            # OpenAI/DeepSeek style
            response = client.chat.completions.create(**body)

        # Compute honest savings from usage
        usage = self._extract_usage(response, provider)
        savings = savings_from_usage(usage, provider)

        # Build the report
        report = SavingsReport(
            provider=provider,
            cached_tokens=savings.cached_tokens,
            uncached_cost=savings.uncached_cost,
            actual_cost=savings.actual_cost,
            savings_pct=savings.savings_pct,
            cache_placement=(
                asdict(cache_plan)
                if cache_plan and provider == "anthropic"
                else None
            ),
            retrieval_applied=bool(retrieval_meta and not retrieval_meta.get("fallback_applied")),
            retrieval_baseline_tokens=(
                retrieval_meta.get("baseline_tokens") if retrieval_meta else None
            ),
            retrieval_optimized_tokens=(
                retrieval_meta.get("optimized_tokens") if retrieval_meta else None
            ),
        )

        return response, report

    def _extract_usage(self, response: Any, provider: str) -> dict:
        """Extract usage info from provider response."""
        if provider == "anthropic":
            return {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": getattr(
                    response.usage, "cache_creation_input_tokens", 0
                ),
                "cache_read_input_tokens": getattr(
                    response.usage, "cache_read_input_tokens", 0
                ),
            }
        else:  # OpenAI / DeepSeek
            details = response.usage.prompt_tokens_details or {}
            return {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "prompt_tokens_details": details,
            }

    def _extract_context_chunks(self, messages: List[Dict[str, str]]) -> List[str]:
        """Extract context chunks from non-latest messages (for retrieval)."""
        chunks = []
        for msg in messages[:-1]:  # Exclude the latest user message (volatile)
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            chunks.append(text)
        return chunks

    def _extract_task(self, messages: List[Dict[str, str]]) -> str:
        """Extract the task/query from the latest user message."""
        if not messages:
            return ""
        latest = messages[-1].get("content", "")
        if isinstance(latest, str):
            return latest[:200]  # First 200 chars as task hint
        if isinstance(latest, list):
            for block in latest:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")[:200]
        return ""

    def _rebuild_messages_with_retrieved(
        self,
        original_messages: List[Dict[str, str]],
        selected_chunks: List[str],
        original_chunks: List[str],
    ) -> List[Dict[str, str]]:
        """Rebuild messages, replacing prior context with retrieved chunks.

        Keeps the latest user message intact; replaces earlier context blocks.
        """
        if not selected_chunks or len(selected_chunks) == len(original_chunks):
            return original_messages  # No actual reduction

        # Simple approach: if a message's content appears in original_chunks,
        # only keep it if it's in selected_chunks
        kept = []
        for msg in original_messages[:-1]:
            content = msg.get("content", "")
            if isinstance(content, str):
                if content in selected_chunks:
                    kept.append(msg)
            else:
                kept.append(msg)  # Keep non-string content for safety

        # Always keep the latest message
        kept.append(original_messages[-1])
        return kept if kept else original_messages
