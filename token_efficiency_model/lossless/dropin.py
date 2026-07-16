"""Drop-in token-savings middleware for OpenAI, Anthropic, and DeepSeek.

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
from .engine import optimize_request, record_usage
from .router import BrevitasRouter


@dataclass
class SavingsReport:
    """Savings from a single chat call."""
    provider: str
    cached_tokens: int
    uncached_cost: float
    actual_cost: float
    savings_pct: float                 # TOTAL incl. output (your real bill cut)
    input_fresh: int = 0               # input tokens billed at full price
    input_cached: int = 0              # input tokens served from cache
    output_tokens: int = 0             # output tokens (never cached, full price)
    input_savings_pct: float = 0.0     # input-only savings (ignores output), for reference
    cache_placement: Optional[Dict[str, Any]] = None  # Anthropic: CachePlan details
    retrieval_applied: bool = False
    retrieval_baseline_tokens: Optional[int] = None
    retrieval_optimized_tokens: Optional[int] = None


class BrevitasDropIn:
    """Drop-in middleware for OpenAI/Anthropic/DeepSeek chat APIs.

    Wraps a base_url + API key and applies token savings:
    - Provider-native caching (Anthropic breakpoints, OpenAI/DeepSeek prefix cache)
    - Experimental retrieval-based context reduction when explicitly enabled
    - Honest savings computation from real usage

    Caching is byte-preserving. Retrieval can omit evidence and is not described as lossless.
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
        # one router per client; auto-decides cache_only vs retrieve per call and learns
        # each provider's real cache-hit rate from responses.
        self._router = BrevitasRouter(provider=(provider or "openai"))
        self._session_seq = 0

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
        # Check if we have a cached client for this specific provider
        if (self._client is not None and
            hasattr(self._client, "__provider__") and
            self._client.__provider__ == provider):
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
        session_id: str = "default",
        **kwargs: Any,
    ) -> Tuple[Any, SavingsReport]:
        """Call the provider's chat API with token savings applied automatically.

        The built-in router uses byte-preserving provider caching by default. It may reduce
        context via retrieval only when ``BREVITAS_RETRIEVAL_ENABLED=1``; that path is
        experimental and should be enabled after a paired workload quality test.

        Args:
            messages: Chat messages (standard OpenAI/Anthropic format).
            model: Model name (used for provider detection if needed).
            session_id: Group calls that share context (e.g. one conversation/agent) so the
                router can detect repetition and learn cache behavior. Default "default".
            **kwargs: All other chat() args (temperature, max_tokens, tools, system, etc.).

        Returns:
            (response, SavingsReport) — honest cost/token metrics from real usage.
        """
        provider = self._detect_provider(model)
        client = self._route_client(provider)

        body = {"messages": list(messages), "model": model, **kwargs}

        # Anthropic requires `system` as a TOP-LEVEL field, not a role in messages.
        # Customers writing OpenAI-style messages (system role in the list) must not 400
        # when wrapped for Anthropic — hoist leading system-role messages into `system`,
        # losslessly (same content, correct field). Merge with any explicit system kwarg.
        if provider == "anthropic":
            sys_texts, rest = [], []
            for m in body["messages"]:
                if m.get("role") == "system":
                    c = m.get("content", "")
                    sys_texts.append(c if isinstance(c, str)
                                     else " ".join(b.get("text", "") for b in c
                                                   if isinstance(b, dict)))
                else:
                    rest.append(m)
            if sys_texts:
                existing = body.get("system")
                existing = [existing] if isinstance(existing, str) else (existing or [])
                if isinstance(existing, list):
                    existing = [e if isinstance(e, str) else str(e) for e in existing]
                body["system"] = "\n\n".join([*sys_texts, *existing]) if existing else "\n\n".join(sys_texts)
                body["messages"] = rest

        # Router-driven optimization (byte-preserving by default; retrieval is explicit opt-in).
        decision = optimize_request(body, provider, self._router, session_id)

        if provider == "anthropic":
            response = client.messages.create(**body)
        else:
            response = client.chat.completions.create(**body)

        # Honest savings + feed real cache-hit rate back to the router
        usage = self._extract_usage(response, provider)
        savings = record_usage(usage, provider, self._router, session_id)

        report = SavingsReport(
            provider=provider,
            cached_tokens=savings.cached_tokens,
            uncached_cost=savings.uncached_cost,
            actual_cost=savings.actual_cost,
            savings_pct=savings.savings_pct,
            input_fresh=savings.input_fresh,
            input_cached=savings.input_cached,
            output_tokens=savings.output_tokens,
            input_savings_pct=savings.input_savings_pct,
            cache_placement={"strategy": decision.get("strategy"),
                             "reason": decision.get("reason"),
                             **{k: v for k, v in decision.items()
                                if k in ("cache_breakpoints", "cached_prefix_tokens", "kept", "of")}},
            retrieval_applied=(decision.get("strategy") == "retrieve"),
            retrieval_baseline_tokens=decision.get("baseline_tokens"),
            retrieval_optimized_tokens=decision.get("optimized_tokens"),
        )
        return response, report

    def optimize_prompt(self, text: str, rate: float = 1.0):
        """Shrink a single prompt's tokens (lossless normalization; rate<1.0 = LLMLingua-2,
        lossy, needs the [promptopt] extra). Returns a PromptOptimization with token counts."""
        from .prompt_optimizer import optimize_prompt as _opt
        return _opt(text, rate=rate)

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
        else:  # OpenAI / DeepSeek — prompt_tokens_details may be a pydantic obj or dict
            details = getattr(response.usage, "prompt_tokens_details", None) or {}
            cached = details.get("cached_tokens", 0) if isinstance(details, dict) \
                else getattr(details, "cached_tokens", 0) or 0
            return {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "prompt_tokens_details": {"cached_tokens": cached},
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
        Never drops assistant or tool turns; only prunes user/context text content.
        """
        if not selected_chunks or len(selected_chunks) == len(original_chunks):
            return original_messages  # No actual reduction

        # Build new message list: preserve all assistant/tool turns;
        # only filter user messages by retrieved content
        kept = []
        for msg in original_messages[:-1]:
            role = msg.get("role", "")

            # Always keep assistant and tool messages (structure integrity)
            if role == "assistant" or role == "tool":
                kept.append(msg)
            elif role == "user":
                # For user messages, check if content is in retrieved chunks
                content = msg.get("content", "")
                if isinstance(content, str):
                    if content in selected_chunks:
                        kept.append(msg)
                elif isinstance(content, list):
                    # For content lists (mixed text/tool_result), check text blocks
                    has_retrieved_text = False
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            if block.get("text", "") in selected_chunks:
                                has_retrieved_text = True
                                break
                    if has_retrieved_text:
                        kept.append(msg)
                else:
                    # Non-string, non-list content: preserve for safety
                    kept.append(msg)
            else:
                # Unknown role: preserve for safety
                kept.append(msg)

        # Always keep the latest message
        kept.append(original_messages[-1])
        return kept if kept else original_messages


# Friendly alias for the importable service
BrevitasClient = BrevitasDropIn
