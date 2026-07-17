"""
Brevitas — add measured token savings between your agents and the model.

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
rate, and reports measured savings. Provider caching is byte-preserving. Context-reducing
retrieval is experimental and requires ``BREVITAS_RETRIEVAL_ENABLED=1`` after a paired
workload quality test.

Quick start (SDK wrapper around an existing client):
    import anthropic, brevitas
    client = brevitas.wrap(anthropic.Anthropic(api_key="sk-ant-..."))

    # All calls are now automatically compressed
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

    # OpenAI detection: has .chat.completions
    if hasattr(client, "chat") and hasattr(getattr(client, "chat", None), "completions"):
        from .wrappers.openai import BrevitasOpenAIClient
        return BrevitasOpenAIClient(client, session=session)

    raise TypeError(
        f"brevitas.wrap() does not recognise client type {type(client).__name__!r}. "
        "Pass an anthropic.Anthropic or openai.OpenAI instance."
    )


__all__ = ["BrevitasClient", "SavingsReport", "BrevitasRouter",
           "configure", "get_config", "wrap", "BrevitasSession",
           "start_run", "agent", "get_pipeline", "get_agent", "get_run_id", "resolve_labels",
           "report_receipt", "optimize_prompt", "PromptOptimization", "TaskCompressionRouter", "classify_task"]
__version__ = "0.9.11"
