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

import asyncio
import inspect
import threading
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
        self._client_provider: Optional[str] = None
        self._lifecycle = threading.Condition(threading.RLock())
        self._transitioning = False
        self._active_calls = 0
        self._shutdown = False
        # one router per client; auto-decides cache_only vs retrieve per call and learns
        # each provider's real cache-hit rate from responses.
        self._router = BrevitasRouter(provider=(provider or "openai"))
        self._session_seq = 0

    @staticmethod
    async def _await_close_result(result: Any) -> None:
        if inspect.isawaitable(result):
            await result

    @classmethod
    def _run_awaitable_sync(cls, result: Any) -> None:
        if not inspect.isawaitable(result):
            return

        def run() -> None:
            try:
                asyncio.run(cls._await_close_result(result))
            except (Exception, asyncio.CancelledError):
                pass

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            run()
            return
        thread = threading.Thread(target=run, name="brevitas-client-close", daemon=False)
        thread.start()
        thread.join()

    @classmethod
    def _close_client_sync(cls, client: Any) -> None:
        """Invoke exactly one available closer without exposing its exception text."""
        close = getattr(client, "close", None)
        if callable(close):
            try:
                cls._run_awaitable_sync(close())
            except (Exception, asyncio.CancelledError):
                pass
            return
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            try:
                cls._run_awaitable_sync(aclose())
            except (Exception, asyncio.CancelledError):
                pass

    @staticmethod
    async def _close_client_async(client: Any) -> None:
        """Prefer the async closer; move a sync closer off the event loop."""
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            try:
                result = aclose()
                if inspect.isawaitable(result):
                    await result
            except (Exception, asyncio.CancelledError):
                pass
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except (Exception, asyncio.CancelledError):
                pass

    def _begin_shutdown(self) -> Any:
        with self._lifecycle:
            while self._transitioning:
                self._lifecycle.wait()
            if self._shutdown and self._client is None:
                return None
            self._shutdown = True
            while self._active_calls:
                self._lifecycle.wait()
            client = self._client
            self._client = None
            self._client_provider = None
            if client is None:
                return None
            self._transitioning = True
            return client

    def _finish_transition(self) -> None:
        with self._lifecycle:
            self._transitioning = False
            self._lifecycle.notify_all()

    def close(self) -> None:
        """Synchronously close the owned provider pool exactly once."""
        client = self._begin_shutdown()
        if client is None:
            return
        try:
            self._close_client_sync(client)
        finally:
            self._finish_transition()

    async def _aclose_impl(self) -> None:
        client = await asyncio.to_thread(self._begin_shutdown)
        if client is None:
            return
        try:
            await self._close_client_async(client)
        finally:
            self._finish_transition()

    async def aclose(self) -> None:
        """Close asynchronously and finish cleanup even if the caller is cancelled."""
        cleanup = asyncio.create_task(self._aclose_impl(), name="brevitas-client-aclose")
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as cancelled:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except (Exception, asyncio.CancelledError):
                pass
            raise cancelled

    def __enter__(self) -> "BrevitasDropIn":
        with self._lifecycle:
            if self._shutdown:
                raise RuntimeError("Brevitas client is closed")
        return self

    def __exit__(self, *_args: Any) -> bool:
        self.close()
        return False

    async def __aenter__(self) -> "BrevitasDropIn":
        with self._lifecycle:
            if self._shutdown:
                raise RuntimeError("Brevitas client is closed")
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        await self.aclose()
        return False

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
        with self._lifecycle:
            while True:
                while self._transitioning:
                    self._lifecycle.wait()
                if self._shutdown:
                    raise RuntimeError("Brevitas client is closed")
                current_provider = self._client_provider or getattr(
                    self._client, "__provider__", None)
                if self._client is not None and current_provider == provider:
                    return self._client
                if self._active_calls:
                    self._lifecycle.wait()
                    continue
                self._transitioning = True
                prior = self._client
                self._client = None
                self._client_provider = None
                break

        client = None
        try:
            if prior is not None:
                self._close_client_sync(prior)
            if provider == "anthropic":
                try:
                    import anthropic
                    client = anthropic.Anthropic(api_key=self.api_key)
                except ImportError:
                    raise ImportError(
                        "anthropic package required; install with: pip install anthropic"
                    )
            else:
                try:
                    import openai
                    client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
                except ImportError:
                    raise ImportError(
                        "openai package required; install with: pip install openai"
                    )
            try:
                client.__provider__ = provider
            except Exception:
                pass
            with self._lifecycle:
                self._client = client
                self._client_provider = provider
            return client
        finally:
            self._finish_transition()

    def _acquire_client(self, provider: str) -> Any:
        while True:
            client = self._route_client(provider)
            with self._lifecycle:
                if self._shutdown:
                    raise RuntimeError("Brevitas client is closed")
                # Compatibility tests and integrations sometimes override
                # ``_route_client`` with a factory that returns, but does not cache,
                # the provider client. Adopt it only while ownership is empty and no
                # replacement is in progress; otherwise retry against the winner.
                if self._client is None and not self._transitioning:
                    self._client = client
                    self._client_provider = provider
                    self._active_calls += 1
                    return client
                if self._client is client and not self._transitioning:
                    self._active_calls += 1
                    return client

    def _release_client(self) -> None:
        with self._lifecycle:
            self._active_calls = max(0, self._active_calls - 1)
            if self._active_calls == 0:
                self._lifecycle.notify_all()

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

        body = {"messages": list(messages), "model": model, **kwargs}

        # Legacy OpenAI-style *leading, plain-text* system messages map to Anthropic's
        # top-level `system`.  Do not hoist mid-conversation or structured system
        # directives: Claude Opus 4.8 supports them and their exact position is semantic.
        if provider == "anthropic":
            sys_texts = []
            leading = 0
            for m in body["messages"]:
                if (not isinstance(m, dict) or m.get("role") != "system"
                        or set(m) - {"role", "content"}
                        or not isinstance(m.get("content", ""), str)):
                    break
                sys_texts.append(m.get("content", ""))
                leading += 1
            if sys_texts:
                existing = body.get("system")
                hoisted = "\n\n".join(sys_texts)
                if isinstance(existing, str) and existing:
                    body["system"] = f"{hoisted}\n\n{existing}"
                elif isinstance(existing, list) and existing:
                    # Preserve caller-owned structured top-level system blocks exactly.
                    body["system"] = [
                        {"type": "text", "text": hoisted},
                        *existing,
                    ]
                elif existing:
                    # Let the provider validate an unusual caller-owned block, but do
                    # not accidentally expand a dict into its keys.
                    body["system"] = [
                        {"type": "text", "text": hoisted}, existing,
                    ]
                else:
                    body["system"] = hoisted
                body["messages"] = body["messages"][leading:]

        # Router-driven optimization (byte-preserving by default; retrieval is explicit opt-in).
        decision = optimize_request(body, provider, self._router, session_id)

        client = self._acquire_client(provider)
        try:
            if provider == "anthropic":
                response = client.messages.create(**body)
            else:
                response = client.chat.completions.create(**body)
        finally:
            self._release_client()

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
