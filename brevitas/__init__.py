"""
Brevitas — add provider-cache optimization and mechanism-separated metering.

Quick start (importable service — recommended):
    from brevitas import BrevitasClient

    client = BrevitasClient(provider="openai", api_key="sk-...")
    response, savings = client.chat(
        messages=[{"role": "system", "content": BRAND_PROMPT},
                  {"role": "user", "content": "Write a tweet for our oak table."}],
        model="gpt-4o", session_id="marketing-agent",
    )
    print(savings.savings_pct, savings.cache_placement["strategy"])

The client keeps cacheable prefixes byte-identical, learns each provider's real cache-hit
rate, and reports provider usage without relabeling cache discounts as removed tokens.
Provider caching is content-preserving. Context-reducing
retrieval is experimental and requires ``BREVITAS_RETRIEVAL_ENABLED=1`` after a paired
workload quality test.

Quick start (SDK wrapper around an existing client):
    import anthropic, brevitas
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))

    # Calls now use content-preserving cache optimization by default.
    # Quality-affecting compression/retrieval remains explicit opt-in.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "..."}],
    )

Quick start (zero-code proxy):
    $ brevitas start --api-key bvt_... --port 4242
    $ export ANTHROPIC_BASE_URL=http://localhost:4242
    # Your existing code works unchanged.
"""
import os

from .config import configure, get as get_config
from .session import BrevitasSession
from .labels import start_run, agent, get_pipeline, get_agent, get_run_id, resolve_labels
from token_efficiency_model.lossless import BrevitasClient, SavingsReport, BrevitasRouter
from token_efficiency_model.lossless import optimize_prompt, PromptOptimization
from token_efficiency_model.lossless import TaskCompressionRouter, classify_task


def report_receipt(provider: str, model: str, baseline_tokens: int, usage: dict,
                   *, operation: str = "chat", quality_score: float | None = None,
                   metadata: dict | None = None) -> dict:
    """Report any AgentMap-detected provider receipt without sending model content."""
    from ._compress import report_usage
    from .receipts import normalize_usage
    labels = resolve_labels(metadata)
    receipt = normalize_usage(usage, provider)
    session = BrevitasSession()
    session.last_quality = quality_score
    # If a provider exposes no token receipt, still count the call without inventing
    # savings: use the local baseline as actual input and leave receipt categories absent.
    receipt_meta = receipt.as_dict() if receipt.total_tokens else {}
    # This hook observes a provider call; it did not transform the request. Use
    # the same local count on both sides so provider-tokenizer differences cannot
    # masquerade as savings. The receipt still anchors actual billed usage/cost.
    report_usage(provider, model, baseline_tokens, baseline_tokens, session,
                 pipeline=labels["pipeline"], agent=labels["agent"], run_id=labels["run_id"],
                 usage_raw=usage, strategy="passthrough:external_receipt",
                 metadata={**labels, **receipt_meta, "operation": operation,
                           "receipt_available": bool(receipt.total_tokens),
                           "receipt_source": "manual"})
    return receipt.as_dict()


def wrap(client, session: BrevitasSession | None = None):
    """
    Wrap an Anthropic or OpenAI client.

    Returns a drop-in replacement that compresses messages before each call
    and tracks multi-hop context within the same pipeline run.

    Args:
        client:  An anthropic.Anthropic or openai.OpenAI instance.
        session: Optional existing BrevitasSession (creates a new one if omitted).
    """
    # Anthropic detection: has .messages attribute with a .create method
    if hasattr(client, "messages") and hasattr(getattr(client, "messages", None), "create"):
        from .wrappers.anthropic import BrevitasAnthropicClient
        return BrevitasAnthropicClient(client, session=session)

    # OpenAI detection: has .chat.completions. Async clients need an async-aware
    # wrapper so awaiting, streaming, and usage reporting retain SDK semantics.
    if hasattr(client, "chat") and hasattr(getattr(client, "chat", None), "completions"):
        import inspect
        create = getattr(getattr(client.chat, "completions", None), "create", None)
        if inspect.iscoroutinefunction(create):
            from .wrappers.openai import BrevitasAsyncOpenAIClient
            return BrevitasAsyncOpenAIClient(client, session=session)
        from .wrappers.openai import BrevitasOpenAIClient
        return BrevitasOpenAIClient(client, session=session)

    raise TypeError(
        f"brevitas.wrap() does not recognise client type {type(client).__name__!r}. "
        "Pass an anthropic.Anthropic or openai.OpenAI instance."
    )


def _looks_like_llm_client(client) -> bool:
    """True for an OpenAI- or Anthropic-style SDK client (same duck-typing as wrap())."""
    messages = getattr(client, "messages", None)
    if messages is not None and hasattr(messages, "create"):
        return True
    chat = getattr(client, "chat", None)
    return chat is not None and hasattr(chat, "completions")


def hosted(client, *, api_key: str | None = None, customer_id: str | None = None,
           base_url: str | None = None):
    """
    Point an existing OpenAI or Anthropic client at the hosted Brevitas gateway.

    This is the billable path: requests route through Brevitas, which forwards them to
    the provider using the caller's OWN provider key (unchanged), meters usage from the
    provider's receipt, and returns the provider's response. ``hosted()`` just assembles
    the gateway base URL and the two required Brevitas headers so you don't hand-write
    them on every client.

    Unlike ``wrap()``, no client-side optimization is applied — the gateway does that
    server-side, so wrapping a hosted client would double-count. Use one or the other.

    Args:
        client:      An ``openai.OpenAI``/``AsyncOpenAI`` or ``anthropic.Anthropic``/
                     ``AsyncAnthropic`` instance, already carrying your PROVIDER key.
        api_key:     Your Brevitas key (``bvt_...``). Falls back to ``BREVITAS_API_KEY``.
        customer_id: Tenant id sent as ``X-Brevitas-Customer-ID``. Falls back to
                     ``BREVITAS_CUSTOMER_ID``. Required for organization-service keys
                     until a default-customer pin ships; omitted from the request when
                     unresolved.
        base_url:    Gateway origin override (bare, no ``/v1``). Falls back to
                     ``BREVITAS_BASE_URL`` then the default hosted origin.

    Returns:
        A configured client (a copy via the SDK's ``with_options``/``copy``); use it
        exactly like the original.

    Raises:
        ValueError: if no Brevitas key can be resolved.
        TypeError:  if ``client`` is not a supported SDK client.

    Example::

        from openai import OpenAI
        import brevitas
        client = brevitas.hosted(OpenAI(), customer_id="acme")   # BREVITAS_API_KEY from env
        client.chat.completions.create(model="gpt-4o-mini",
                                       messages=[{"role": "user", "content": "ping"}])
    """
    from .config import DEFAULT_BASE_URL
    from .identity import KEY_HEADER, CUSTOMER_HEADER

    resolved_key = (api_key or os.getenv("BREVITAS_API_KEY") or "").strip()
    if not resolved_key:
        raise ValueError(
            "brevitas.hosted() needs your Brevitas key. Pass api_key='bvt_...' or set the "
            "BREVITAS_API_KEY environment variable."
        )
    if not _looks_like_llm_client(client):
        raise TypeError(
            f"brevitas.hosted() does not recognise client type {type(client).__name__!r}. "
            "Pass an openai.OpenAI or anthropic.Anthropic instance."
        )
    reconfigure = getattr(client, "with_options", None) or getattr(client, "copy", None)
    if not callable(reconfigure):
        raise TypeError(
            "brevitas.hosted() needs an SDK client exposing .with_options()/.copy() "
            f"(got {type(client).__name__!r}); set base_url and default_headers at "
            "construction instead."
        )

    resolved_customer = (customer_id or os.getenv("BREVITAS_CUSTOMER_ID") or "").strip()
    origin = (base_url or os.getenv("BREVITAS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    # The client talks to the gateway under /v1 (the SDK appends the rest of the path).
    # This is deliberately distinct from the bare-origin BREVITAS_BASE_URL the control-
    # plane SDK uses for report_usage(), which must NOT carry a /v1 suffix.
    gateway = origin if origin.endswith("/v1") else f"{origin}/v1"

    headers = {KEY_HEADER: resolved_key}
    if resolved_customer:
        headers[CUSTOMER_HEADER] = resolved_customer

    # with_options/copy MERGES default_headers over the client's existing ones, so any
    # headers the caller already set are preserved and the provider key (Authorization,
    # carried as the client's own api_key) is untouched.
    return reconfigure(base_url=gateway, default_headers=headers)


__all__ = ["BrevitasClient", "SavingsReport", "BrevitasRouter",
           "configure", "get_config", "wrap", "hosted", "BrevitasSession",
           "start_run", "agent", "get_pipeline", "get_agent", "get_run_id", "resolve_labels",
           "report_receipt", "optimize_prompt", "PromptOptimization", "TaskCompressionRouter", "classify_task"]
__version__ = "0.9.11"
